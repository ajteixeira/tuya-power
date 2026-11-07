"""Autenticação: login OIDC (Auth0, Google... com Authorization Code + PKCE e validação do id_token em JWT)
quando configurado; senão palavra-passe (HTTP Basic). Mais a regra de «rede local confiável» (opcional).

Fecha por omissão: sem credenciais válidas, sem sessão OIDC e sem LAN_TRUSTED=true ninguém entra. Na regra
LAN basta um indício de acesso externo (https, host desconhecido, IP público) para exigir login.
"""
import base64
import hashlib
import ipaddress
import asyncio
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import users
from .config import settings

router = APIRouter(prefix="/auth")
log = logging.getLogger("uvicorn.error")
templates = None  # definido por main.py (mesmas globais/filtros das restantes páginas)

basic = HTTPBasic(auto_error=False)
BASIC_CHALLENGE = {"WWW-Authenticate": 'Basic realm="Tuya Power"'}
_cache: dict = {}  # descoberta OIDC e JWKS (1 hora)
CACHE_SECONDS = 3600


def http_client() -> httpx.AsyncClient:  # os testes substituem-no
    return httpx.AsyncClient(timeout=15)


def password_enabled() -> bool:
    """A palavra-passe (HTTP Basic) só vale quando o login OIDC NÃO está configurado."""
    return bool(settings.web_password) and not oidc_enabled()


def password_ok(cred: HTTPBasicCredentials | None) -> bool:
    return bool(cred and password_enabled()
                and secrets.compare_digest(cred.username.encode(), settings.web_user.encode())
                and secrets.compare_digest(cred.password.encode(), settings.web_password.encode()))


def oidc_enabled() -> bool:
    return bool(settings.oidc_issuer.startswith("https://") and settings.oidc_client_id and settings.oidc_client_secret)


def admin_emails() -> set[str]:
    """Emails que entram como Admin ativos. OIDC_ALLOWED_EMAILS (descontinuado) conta como ADMIN_EMAILS."""
    raw = f"{settings.admin_emails},{settings.oidc_allowed_emails}"
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


async def _get_json(url: str) -> dict:
    async with http_client() as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.json()


async def oidc_meta() -> dict:
    """Descoberta OIDC ({issuer}/.well-known/openid-configuration), em cache."""
    hit = _cache.get("meta")
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    meta = await _get_json(settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration")
    for k in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not meta.get(k):
            raise ValueError(f"descoberta OIDC sem {k}")
    _cache["meta"] = (time.time(), meta)
    return meta


async def signing_key(kid: str | None, meta: dict, refresh: bool = False):
    """Chave pública do JWKS com o kid do token; se não existir, volta a obter o JWKS uma vez (rotação)."""
    hit = _cache.get("jwks")
    if refresh or not hit or time.time() - hit[0] >= CACHE_SECONDS:
        _cache["jwks"] = hit = (time.time(), (await _get_json(meta["jwks_uri"])).get("keys", []))
    for k in hit[1]:
        if k.get("kid") == kid and k.get("kty") == "RSA":
            return jwt.PyJWK.from_dict(k).key
    if not refresh:
        return await signing_key(kid, meta, refresh=True)
    raise KeyError("chave de assinatura desconhecida")


async def verify_id_token(token: str, nonce: str, meta: dict) -> dict:
    """Valida o id_token (JWT): só RS256 (recusa none/HS256), assinatura pelo JWKS, iss, aud, exp, iat e nonce."""
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256":
        raise ValueError("algoritmo não permitido")
    key = await signing_key(header.get("kid"), meta)
    claims = jwt.decode(token, key, algorithms=["RS256"], audience=settings.oidc_client_id, issuer=meta["issuer"],
                        options={"require": ["exp", "iat", "iss", "aud", "sub"]}, leeway=60)
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("nonce inválido")
    return claims


def session_secret() -> str:
    """SECRET_KEY, ou um segredo gerado e guardado ao lado da base de dados (modo 600)."""
    if settings.secret_key:
        return settings.secret_key
    f = Path(settings.db_path).parent / ".session_secret"
    try:
        if f.is_file() and f.read_text().strip():
            return f.read_text().strip()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
        return f.read_text().strip()
    except OSError:  # pasta só de leitura: segredo por processo (as sessões caem ao reiniciar)
        return secrets.token_hex(32)


def _private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """IP do cliente. O X-Forwarded-For só vale se o par TCP for o proxy (loopback/privado); usa-se o ÚLTIMO
    valor, o acrescentado pelo proxy de confiança e não o que o cliente possa ter forjado."""
    peer = request.client.host if request.client else ""
    if _private(peer):
        xff = request.headers.get("x-forwarded-for", "")
        if xff.strip():
            return xff.split(",")[-1].strip()
    return peer


def is_lan(request: Request) -> bool:
    """Rede local confiável: opt-in e TODOS os indícios de «local» (http, host da LAN, IP privado)."""
    if not settings.lan_trusted:
        return False
    if request.url.scheme != "http":
        return False
    if request.headers.get("x-forwarded-proto", "http").split(",")[-1].strip().lower() != "http":
        return False
    if "https" in request.headers.get("forwarded", "").lower():
        return False
    host = request.headers.get("host", "").split(":")[0].lower()
    if host not in {h.strip().lower() for h in settings.lan_hosts.split(",") if h.strip()}:
        return False
    return _private(client_ip(request))


def prefix(request: Request) -> str:
    return request.scope.get("tp_prefix", "")


def _redirect_uri(request: Request) -> str:
    if settings.auth_redirect_uri:
        return settings.auth_redirect_uri
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[-1].strip()
    return f"{proto}://{request.headers.get('host', '')}{prefix(request)}/auth/callback"


async def require_login(request: Request, cred: HTTPBasicCredentials | None = Depends(basic)):
    """Dependência global: identifica quem pede e guarda-o em request.state.user ({email, name, role, source}).
    Sessão OIDC (utilizador ativo na base) > rede local confiável (papel user) > palavra-passe (papel admin)."""
    path = request.scope["path"]
    if path.startswith("/auth/"):
        return
    uid = request.session.get("uid")
    if uid:
        u = users.by_id(uid)  # relê a base: um Admin pode ter desativado a conta entretanto
        if u and u["status"] == "active":
            request.state.user = {"id": u["id"], "email": u["email"], "name": u["name"] or u["email"],
                                  "role": u["role"], "source": "oidc"}
            return
        request.session.clear()
    if is_lan(request):
        request.state.user = {"email": "rede local", "name": "rede local", "role": "user", "source": "lan"}
        return
    if password_ok(cred):
        request.state.user = {"email": cred.username, "name": cred.username, "role": "admin", "source": "password"}
        return
    if not oidc_enabled() and password_enabled():  # só palavra-passe: o navegador pede-a (HTTP Basic)
        raise HTTPException(401, "Autenticação necessária", headers=BASIC_CHALLENGE)
    # Com OIDC, sem sessão vai-se direto ao fornecedor (sem página/botão intermédio); sem OIDC, à página que avisa
    login = f"{prefix(request)}/auth/{'sso' if oidc_enabled() else 'login'}"
    if request.headers.get("hx-request"):  # fragmentos HTMX: o navegador redireciona a página inteira
        raise HTTPException(401, "Sessão necessária", headers={"HX-Redirect": login})
    if "text/html" not in request.headers.get("accept", ""):  # fetch/JSON
        raise HTTPException(401, "Sessão necessária")
    if request.method == "GET":
        request.session["next"] = path + (f"?{request.url.query}" if request.url.query else "")
    raise HTTPException(303, "Sessão necessária", headers={"Location": login})


def _page(request: Request, status: int, **ctx):
    return templates.TemplateResponse(request, "login.html", {"oidc": oidc_enabled(), "oidc_name": settings.oidc_name, "password": password_enabled(), **ctx},
                                      status_code=status)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", msg: str = ""):
    """Só mostra uma página quando há algo a dizer (recusa, erro, sessão terminada); sem mensagem, e com OIDC,
    segue direto para o fornecedor."""
    if request.session.get("uid"):
        return RedirectResponse(prefix(request) + "/", 303)
    if oidc_enabled() and not error and not msg:
        return RedirectResponse(prefix(request) + "/auth/sso", 303)
    errors = {"denied": "O acesso desta conta foi recusado por um administrador.",
              "disabled": "Esta conta está desativada.",
              "unverified": "Confirme primeiro o seu email: o fornecedor enviou-lhe uma ligação de verificação.",
              "state": "O pedido de login expirou. Tente de novo.",
              "idp": "O fornecedor de login recusou ou falhou (ou o token é inválido)."}
    info = {"bye": "Sessão terminada.",
            "pending": "O seu pedido de acesso foi registado e aguarda a aprovação de um administrador. Volte mais tarde."}
    return _page(request, 200, error=errors.get(error, ""), info=info.get(msg, ""))


@router.get("/sso")
async def start(request: Request, prompt: str = ""):
    """prompt=login obriga o fornecedor a pedir as credenciais, mesmo que a sessão dele ainda esteja aberta."""
    if not oidc_enabled():
        raise HTTPException(403, "Login OIDC não configurado")
    login = prefix(request) + "/auth/login"
    try:
        meta = await oidc_meta()
    except (httpx.HTTPError, ValueError, KeyError):
        return RedirectResponse(f"{login}?error=idp", 303)
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    request.session["oauth"] = {"state": state, "nonce": nonce, "verifier": verifier}
    qs = urlencode({"client_id": settings.oidc_client_id, "redirect_uri": _redirect_uri(request),
                    "response_type": "code", "scope": settings.oidc_scopes, "state": state, "nonce": nonce,
                    "code_challenge": challenge, "code_challenge_method": "S256",
                    **({"prompt": "login"} if prompt == "login" else {})})
    return RedirectResponse(f"{meta['authorization_endpoint']}?{qs}", 303)


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    login = prefix(request) + "/auth/login"
    oauth = request.session.pop("oauth", None)
    if error:
        return RedirectResponse(f"{login}?error=idp", 303)
    if not oauth or not state or not secrets.compare_digest(oauth["state"], state) or not code:
        return RedirectResponse(f"{login}?error=state", 303)
    try:
        meta = await oidc_meta()
        async with http_client() as c:
            tok = await c.post(meta["token_endpoint"], data={
                "code": code, "client_id": settings.oidc_client_id, "client_secret": settings.oidc_client_secret,
                "redirect_uri": _redirect_uri(request), "grant_type": "authorization_code",
                "code_verifier": oauth["verifier"]})
            tok.raise_for_status()
            claims = await verify_id_token(tok.json()["id_token"], oauth["nonce"], meta)
    except (httpx.HTTPError, jwt.PyJWTError, KeyError, ValueError):
        return RedirectResponse(f"{login}?error=idp", 303)
    email = str(claims.get("email", "")).lower()
    if not email:
        log.warning("Login OIDC recusado: sem email no token (scope email?)")
        return RedirectResponse(f"{login}?error=denied", 303)
    if claims.get("email_verified") is not True and settings.oidc_require_verified_email:
        log.warning("Login OIDC recusado: email não verificado (%s)", email)
        return RedirectResponse(f"{login}?error=unverified", 303)
    u = users.upsert_login(email, claims.get("name", ""), admin_emails())
    if u["status"] != "active":
        log.warning("Login OIDC sem acesso: %s (%s)", email, u["status"])
        outcome = {"pending": "msg=pending", "rejected": "error=denied", "disabled": "error=disabled"}[u["status"]]
        return RedirectResponse(f"{login}?{outcome}", 303)
    nxt = request.session.pop("next", "/")
    request.session["uid"] = u["id"]
    if not nxt.startswith("/") or nxt.startswith("//"):  # só caminhos locais
        nxt = "/"
    return RedirectResponse(prefix(request) + nxt, 303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(prefix(request) + "/auth/login?msg=bye", 303)

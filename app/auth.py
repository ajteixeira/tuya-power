"""Autenticação: login Google (OAuth2 Authorization Code + PKCE) e regra de «rede local confiável».

Fecha por omissão: sem sessão Google válida, só entra quem vier da rede local em HTTP e apenas com
LAN_TRUSTED=true. Basta um indício de acesso externo (https, host desconhecido, IP público) para exigir login.
"""
import base64
import hashlib
import ipaddress
import secrets
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import settings

router = APIRouter(prefix="/auth")
templates = None  # definido por main.py (mesmas globais/filtros das restantes páginas)

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def http_client() -> httpx.AsyncClient:  # os testes substituem-no
    return httpx.AsyncClient(timeout=15)


def google_enabled() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def allowed_emails() -> set[str]:
    return {e.strip().lower() for e in settings.google_allowed_emails.split(",") if e.strip()}


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


async def require_login(request: Request):
    """Dependência global: deixa passar a LAN confiável e as sessões Google; senão pede o login."""
    path = request.scope["path"]
    if path.startswith("/auth/"):
        return
    if is_lan(request) or request.session.get("user"):
        return
    login = f"{prefix(request)}/auth/login"
    if request.headers.get("hx-request"):  # fragmentos HTMX: o navegador redireciona a página inteira
        raise HTTPException(401, "Sessão necessária", headers={"HX-Redirect": login})
    if "text/html" not in request.headers.get("accept", ""):  # fetch/JSON
        raise HTTPException(401, "Sessão necessária")
    if request.method == "GET":
        request.session["next"] = path + (f"?{request.url.query}" if request.url.query else "")
    raise HTTPException(303, "Sessão necessária", headers={"Location": login})


def _page(request: Request, status: int, **ctx):
    return templates.TemplateResponse(request, "login.html", {"google": google_enabled(), **ctx}, status_code=status)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if request.session.get("user"):
        return RedirectResponse(prefix(request) + "/", 303)
    msgs = {"denied": "Esta conta Google não tem acesso.", "state": "O pedido de login expirou. Tente de novo.",
            "google": "A Google recusou ou falhou o login."}
    return _page(request, 200, error=msgs.get(error, ""))


@router.get("/google")
async def start(request: Request):
    if not google_enabled():
        raise HTTPException(403, "Login Google não configurado")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    request.session["oauth"] = {"state": state, "verifier": verifier}
    qs = urlencode({"client_id": settings.google_client_id, "redirect_uri": _redirect_uri(request),
                    "response_type": "code", "scope": "openid email profile", "state": state,
                    "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"})
    return RedirectResponse(f"{GOOGLE_AUTH}?{qs}", 303)


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    login = prefix(request) + "/auth/login"
    oauth = request.session.pop("oauth", None)
    if error:
        return RedirectResponse(f"{login}?error=google", 303)
    if not oauth or not state or not secrets.compare_digest(oauth["state"], state) or not code:
        return RedirectResponse(f"{login}?error=state", 303)
    try:
        async with http_client() as c:
            tok = await c.post(GOOGLE_TOKEN, data={
                "code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                "redirect_uri": _redirect_uri(request), "grant_type": "authorization_code",
                "code_verifier": oauth["verifier"]})
            tok.raise_for_status()
            info = await c.get(GOOGLE_USERINFO, headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
            info.raise_for_status()
            user = info.json()
    except (httpx.HTTPError, KeyError, ValueError):
        return RedirectResponse(f"{login}?error=google", 303)
    email = str(user.get("email", "")).lower()
    if not (email and user.get("email_verified") and email in allowed_emails()):  # lista vazia nega tudo
        return RedirectResponse(f"{login}?error=denied", 303)
    nxt = request.session.pop("next", "/")
    request.session["user"] = {"email": email, "name": user.get("name", email)}
    if not nxt.startswith("/") or nxt.startswith("//"):  # só caminhos locais
        nxt = "/"
    return RedirectResponse(prefix(request) + nxt, 303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(prefix(request) + "/auth/login", 303)

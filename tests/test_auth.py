"""Testes da autenticação: palavra-passe, login OIDC (Auth0/Google simulado, id_token em JWT assinado com uma
chave RSA gerada aqui) e regra da rede local. Sem rede. Uso: python tests/test_auth.py"""
import base64
import re
import sys
import time
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
import app.main as m

ok_n = fail_n = 0
def check(name, cond):
    global ok_n, fail_n
    ok_n += bool(cond); fail_n += (not cond)
    print(("OK   " if cond else "FALHA"), name)

ISS = "https://tenant.test/"
settings.oidc_issuer, settings.oidc_client_id, settings.oidc_client_secret = "https://tenant.test", "cid", "sec"
settings.oidc_allowed_emails = "ok@x.com"
settings.oidc_require_verified_email = True
settings.lan_trusted = False
settings.web_password = ""        # isola os testes do .env local
HTML = {"accept": "text/html"}

def newkey(): return rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY, OTHER = newkey(), newkey()
def jwk(key, kid="k1"):
    d = __import__("json").loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**d, "kid": kid, "use": "sig", "alg": "RS256"}

class Idp:
    """IdP simulado: descoberta, JWKS e endpoint de token. Os campos mudam-se por teste."""
    def __init__(self): self.reset()
    def reset(self):
        auth._cache.clear()
        self.keys = [jwk(KEY)]; self.sign_key = KEY; self.kid = "k1"; self.alg = "RS256"
        self.claims = {}; self.fail_discovery = False; self.token_status = 200; self.jwks_calls = 0
        self.nonce = ""
    def token(self):
        now = int(time.time())
        c = {"iss": ISS, "aud": "cid", "sub": "u1", "iat": now, "exp": now + 600, "nonce": self.nonce,
             "email": "ok@x.com", "email_verified": True, "name": "N", **self.claims}
        c = {k: v for k, v in c.items() if v is not None}
        if self.alg == "none": return jwt.encode(c, None, algorithm="none", headers={"kid": self.kid})
        if self.alg == "HS256": return jwt.encode(c, "x" * 40, algorithm="HS256", headers={"kid": self.kid})
        return jwt.encode(c, self.sign_key, algorithm="RS256", headers={"kid": self.kid})
    def handler(self, req):
        p = req.url.path
        if p.endswith("openid-configuration"):
            if self.fail_discovery: return httpx.Response(500)
            return httpx.Response(200, json={"issuer": ISS, "authorization_endpoint": "https://tenant.test/authorize",
                                             "token_endpoint": "https://tenant.test/oauth/token", "jwks_uri": "https://tenant.test/jwks"})
        if p == "/jwks":
            self.jwks_calls += 1; return httpx.Response(200, json={"keys": self.keys})
        if p == "/oauth/token":
            return httpx.Response(self.token_status, json={"id_token": self.token(), "access_token": "t"})
        return httpx.Response(404)
idp = Idp()
auth.http_client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))

def client(peer="203.0.113.9"): return TestClient(m.app, base_url="http://x", client=(peer, 1234))

def login(c, **over):
    """Percorre /auth/sso -> callback; devolve a resposta do callback."""
    r = c.get("/auth/sso", follow_redirects=False)
    q = dict(x.split("=", 1) for x in r.headers["location"].split("?", 1)[1].split("&"))
    idp.nonce = q["nonce"]
    return c.get(f"/auth/callback?code=abc&state={q['state']}", follow_redirects=False), r

def denied_with(label, setup, expect="error=idp"):
    idp.reset(); setup(); c = client()
    r, _ = login(c)
    check(f"{label} -> recusado ({expect})", expect in r.headers["location"] and c.get("/", headers=HTML, follow_redirects=False).status_code == 303)

# --- sem sessão ---
c = client()
r = c.get("/", headers=HTML, follow_redirects=False)
check("HTML sem sessão -> 303 para /auth/login", r.status_code == 303 and r.headers["location"].endswith("/auth/login"))
r = c.get("/device/x/status", headers={"hx-request": "true"})
check("HTMX sem sessão -> 401 + HX-Redirect", r.status_code == 401 and "hx-redirect" in r.headers)
check("fetch/JSON sem sessão -> 401", c.get("/device/x/codes").status_code == 401)
r = c.get("/auth/login")
check("página de login com o botão do fornecedor", r.status_code == 200 and "Entrar com SSO" in r.text)
idp.reset()
r = c.get("/auth/sso", follow_redirects=False)
loc = r.headers["location"]
check("início do fluxo -> /authorize com state, nonce e PKCE S256",
      loc.startswith("https://tenant.test/authorize?") and all(k in loc for k in ("state=", "nonce=", "code_challenge_method=S256")))
check("redirect_uri deduzido do Host (http://x/auth/callback)", "redirect_uri=http%3A%2F%2Fx%2Fauth%2Fcallback" in loc)
r = c.get("/auth/callback?code=abc&state=errado", follow_redirects=False)
check("state errado -> erro", "error=state" in r.headers["location"])
check("state já consumido -> erro", "error=state" in c.get("/auth/callback?code=abc&state=errado", follow_redirects=False).headers["location"])

# --- login válido ---
idp.reset(); c = client()
c.get("/device/bf5f3d3e4fdf98d25fzbqe", headers=HTML, follow_redirects=False)  # guarda o destino
r, _ = login(c)
check("login válido -> 303 para o destino guardado", r.status_code == 303 and r.headers["location"] == "/device/bf5f3d3e4fdf98d25fzbqe")
check("com sessão: / -> 200", c.get("/").status_code == 200)
check("cabeçalho mostra o email e 'Terminar sessão'", "ok@x.com" in c.get("/").text and "Terminar sessão" in c.get("/").text)
r = c.post("/auth/logout", follow_redirects=False)
check("logout limpa a sessão", r.status_code == 303 and c.get("/", headers=HTML, follow_redirects=False).status_code == 303)

# --- validação do JWT ---
denied_with("nonce errado", lambda: idp.claims.update(nonce="outro"))
denied_with("audiência errada", lambda: idp.claims.update(aud="outro-cliente"))
denied_with("emissor errado", lambda: idp.claims.update(iss="https://falso.test/"))
denied_with("token expirado", lambda: idp.claims.update(exp=int(time.time()) - 3600))
denied_with("sem exp", lambda: idp.claims.update(exp=None))
denied_with("assinado por outra chave (mesmo kid)", lambda: setattr(idp, "sign_key", OTHER))
denied_with("alg=none", lambda: setattr(idp, "alg", "none"))
denied_with("alg=HS256 (confusão de algoritmo)", lambda: setattr(idp, "alg", "HS256"))
denied_with("kid desconhecido (e JWKS sem essa chave)", lambda: setattr(idp, "kid", "inexistente"))
denied_with("endpoint de token falha", lambda: setattr(idp, "token_status", 400))
idp.reset(); idp.fail_discovery = True; c = client()
check("descoberta OIDC falha -> error=idp", "error=idp" in c.get("/auth/sso", follow_redirects=False).headers["location"])
# rotação de chaves: o JWKS em cache não tem o kid novo -> volta a obter e aceita
idp.reset(); c = client(); login(c)                       # enche a cache com k1
idp.keys = [jwk(OTHER, "k2")]; idp.sign_key = OTHER; idp.kid = "k2"; n0 = idp.jwks_calls
r, _ = login(c)
check("rotação de chaves: kid novo -> JWKS obtido de novo e login aceite", r.headers["location"] == "/" and idp.jwks_calls == n0 + 1)

# --- autorização ---
denied_with("email fora da lista", lambda: idp.claims.update(email="outro@x.com"), "error=denied")
denied_with("email não verificado", lambda: idp.claims.update(email_verified=False), "error=denied")
denied_with("sem claim de email", lambda: idp.claims.update(email=None), "error=denied")
settings.oidc_allowed_emails = ""
denied_with("lista vazia nega tudo", lambda: None, "error=denied")
settings.oidc_allowed_emails = "ok@x.com"
settings.oidc_require_verified_email = False
idp.reset(); idp.claims.update(email_verified=False); c = client(); r, _ = login(c)
check("OIDC_REQUIRE_VERIFIED_EMAIL=false aceita email não verificado se estiver na lista", r.headers["location"] == "/")
settings.oidc_require_verified_email = True
r = client().get("/auth/callback?error=access_denied", follow_redirects=False)
check("utilizador cancela no fornecedor -> error=idp", "error=idp" in r.headers["location"])
settings.oidc_issuer = "http://tenant.test"
check("emissor sem https -> OIDC desligado", not auth.oidc_enabled() and client().get("/auth/sso", follow_redirects=False).status_code == 403)
settings.oidc_issuer = "https://tenant.test"

# --- regra LAN ---
settings.lan_trusted = True
def lan(host="lambdacore", peer="127.0.0.1", xff=None, proto=None, fwd=None):
    h = {"host": host, **HTML}
    if xff is not None: h["x-forwarded-for"] = xff
    if proto: h["x-forwarded-proto"] = proto
    if fwd: h["forwarded"] = fwd
    return TestClient(m.app, base_url=f"http://{host}", client=(peer, 5000)).get("/", headers=h, follow_redirects=False).status_code
check("LAN: host local + http + IP privado -> livre", lan(xff="192.168.1.5") == 200)
check("LAN: IP público no X-Forwarded-For -> login", lan(xff="8.8.8.8") == 303)
check("LAN: host desconhecido -> login", lan(host="jtx.ddns.net", xff="192.168.1.5") == 303)
check("LAN: X-Forwarded-Proto https -> login", lan(xff="192.168.1.5", proto="https") == 303)
check("LAN: Forwarded proto=https -> login", lan(xff="192.168.1.5", fwd="for=1.2.3.4;proto=https") == 303)
check("LAN: XFF forjado ('privado, público') usa o último -> login", lan(xff="192.168.1.5, 8.8.8.8") == 303)
check("LAN: XFF 'público, privado' (último acrescentado pelo proxy) -> livre", lan(xff="8.8.8.8, 192.168.1.5") == 200)
check("LAN: par TCP público ignora o XFF forjado -> login", lan(peer="8.8.8.8", xff="192.168.1.5") == 303)
check("LAN: ligação direta de IP privado sem proxy -> livre", lan(peer="192.168.1.20") == 200)
settings.lan_trusted = False
check("LAN desligada (predefinição) -> login", lan(xff="192.168.1.5") == 303)

# --- sem OIDC configurado ---
settings.oidc_client_id = ""
c = client()
check("sem OIDC nem palavra-passe: / pede login (nunca aberto)", c.get("/", headers=HTML, follow_redirects=False).status_code == 303)
check("sem OIDC: página de login avisa", "não está configurado" in c.get("/auth/login").text)
check("sem OIDC: /auth/sso -> 403", c.get("/auth/sso", follow_redirects=False).status_code == 403)

# --- palavra-passe (HTTP Basic) ---
def basic(u, p): return {"authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
settings.web_user, settings.web_password = "admin", "segredo"
c = client()
r = c.get("/", headers=HTML)
check("só palavra-passe: sem credenciais -> 401 + WWW-Authenticate Basic", r.status_code == 401 and "basic" in r.headers.get("www-authenticate", "").lower())
check("só palavra-passe: credenciais certas -> 200", c.get("/", headers={**HTML, **basic("admin", "segredo")}).status_code == 200)
check("só palavra-passe: palavra-passe errada -> 401", c.get("/", headers={**HTML, **basic("admin", "errada")}).status_code == 401)
check("só palavra-passe: utilizador errado -> 401", c.get("/", headers={**HTML, **basic("outro", "segredo")}).status_code == 401)
check("só palavra-passe: fetch/JSON com credenciais -> não 401", c.get("/device/x/codes", headers=basic("admin", "segredo")).status_code != 401)
check("só palavra-passe: HTMX sem credenciais -> 401", c.get("/device/x/status", headers={"hx-request": "true"}).status_code == 401)
settings.oidc_client_id = "cid"   # palavra-passe + OIDC
c = client()
check("com OIDC ativo: sem credenciais -> redireciona para o login", c.get("/", headers=HTML, follow_redirects=False).status_code == 303)
check("página de login mostra o fornecedor e a palavra-passe", all(t in c.get("/auth/login").text for t in ("Entrar com SSO", "palavra-passe")))
check("/auth/basic sem credenciais -> 401", c.get("/auth/basic", follow_redirects=False).status_code == 401)
check("/auth/basic com palavra-passe errada -> 401", c.get("/auth/basic", headers=basic("admin", "errada"), follow_redirects=False).status_code == 401)
r = c.get("/auth/basic", headers=basic("admin", "segredo"), follow_redirects=False)
check("/auth/basic com credenciais certas -> sessão e 303", r.status_code == 303 and c.get("/").status_code == 200)
settings.web_password = ""

print(f"\n{ok_n} ok, {fail_n} falhas")
sys.exit(1 if fail_n else 0)

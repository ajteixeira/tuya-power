"""Testes da autenticação (Google simulada por httpx.MockTransport). Uso: python tests/test_auth.py"""
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient
from app.config import settings
from app import auth
import app.main as m

ok_n = fail_n = 0
def check(name, cond):
    global ok_n, fail_n
    ok_n += cond; fail_n += (not cond)
    print(("OK   " if cond else "FALHA"), name)

settings.google_client_id, settings.google_client_secret = "cid", "sec"
settings.google_allowed_emails = "ok@x.com"
settings.lan_trusted = False
HTML = {"accept": "text/html"}

def mock(email="ok@x.com", verified=True, token_ok=True):
    def handler(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200 if token_ok else 400, json={"access_token": "t"})
        return httpx.Response(200, json={"email": email, "email_verified": verified, "name": "N"})
    auth.http_client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))

def login_flow(c, **kw):
    mock(**kw)
    r = c.get("/auth/google", follow_redirects=False)
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    return c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False), r

c = TestClient(m.app, base_url="http://x", client=("203.0.113.9", 1234))
# --- sem sessão ---
r = c.get("/", headers=HTML, follow_redirects=False)
check("HTML sem sessão -> 303 para /auth/login", r.status_code == 303 and r.headers["location"].endswith("/auth/login"))
r = c.get("/device/x/status", headers={"hx-request": "true"})
check("HTMX sem sessão -> 401 + HX-Redirect", r.status_code == 401 and "hx-redirect" in r.headers)
r = c.get("/device/x/codes")
check("fetch/JSON sem sessão -> 401", r.status_code == 401)
r = c.get("/auth/login")
check("página de login com botão Google", r.status_code == 200 and "Entrar com Google" in r.text)
r = c.get("/auth/google", follow_redirects=False)
loc = r.headers["location"]
check("início do fluxo -> Google com state e PKCE S256", loc.startswith("https://accounts.google.com/") and "code_challenge_method=S256" in loc and "state=" in loc)
r = c.get("/auth/callback?code=abc&state=errado", follow_redirects=False)
check("state errado -> erro", r.status_code == 303 and "error=state" in r.headers["location"])
r = c.get("/auth/callback?code=abc&state=errado", follow_redirects=False)
check("state já consumido (sem /auth/google) -> erro", "error=state" in r.headers["location"])

# --- login com sucesso ---
c.get("/device/bf5f3d3e4fdf98d25fzbqe", headers=HTML, follow_redirects=False)   # guarda o destino
r, _ = login_flow(c)
check("login válido -> 303 para o destino guardado", r.status_code == 303 and r.headers["location"] == "/device/bf5f3d3e4fdf98d25fzbqe")
check("com sessão: / -> 200", c.get("/").status_code == 200)
check("com sessão: cabeçalho mostra o email e 'Terminar sessão'", "ok@x.com" in c.get("/").text and "Terminar sessão" in c.get("/").text)
check("com sessão: /auth/login redireciona para /", c.get("/auth/login", follow_redirects=False).status_code == 303)
r = c.post("/auth/logout", follow_redirects=False)
check("logout -> sessão limpa", r.status_code == 303 and c.get("/", headers=HTML, follow_redirects=False).status_code == 303)

# --- recusas ---
for name, kw, lst in [("email fora da lista", dict(email="outro@x.com"), "ok@x.com"),
                      ("email não verificado", dict(verified=False), "ok@x.com"),
                      ("lista vazia nega tudo", dict(), "")]:
    settings.google_allowed_emails = lst
    cc = TestClient(m.app, base_url="http://x", client=("203.0.113.9", 1))
    r, _ = login_flow(cc, **kw)
    check(f"{name} -> denied e sem acesso", "error=denied" in r.headers["location"] and cc.get("/", headers=HTML, follow_redirects=False).status_code == 303)
settings.google_allowed_emails = "ok@x.com"
cc = TestClient(m.app, base_url="http://x", client=("203.0.113.9", 1))
r, _ = login_flow(cc, token_ok=False)
check("Google recusa o token -> error=google", "error=google" in r.headers["location"])
cc = TestClient(m.app, base_url="http://x", client=("203.0.113.9", 1))
r = cc.get("/auth/callback?error=access_denied", follow_redirects=False)
check("utilizador cancela na Google -> error=google", "error=google" in r.headers["location"])
r = TestClient(m.app, base_url="http://x").get("/auth/login")
check("HTMX/JS não vazam: /auth/login nunca devolve a página protegida", "Entrar com Google" in r.text)

# --- regra LAN ---
settings.lan_trusted = True
def lan(host="lambdacore", peer="127.0.0.1", xff=None, proto=None, fwd=None):
    h = {"host": host, **HTML}
    if xff is not None: h["x-forwarded-for"] = xff
    if proto: h["x-forwarded-proto"] = proto
    if fwd: h["forwarded"] = fwd
    cl = TestClient(m.app, base_url=f"http://{host}", client=(peer, 5000))
    return cl.get("/", headers=h, follow_redirects=False).status_code
check("LAN: host local + http + IP privado -> livre", lan(xff="192.168.1.5") == 200)
check("LAN: IP público no X-Forwarded-For -> login", lan(xff="8.8.8.8") == 303)
check("LAN: host desconhecido (jtx.ddns.net) -> login", lan(host="jtx.ddns.net", xff="192.168.1.5") == 303)
check("LAN: X-Forwarded-Proto https -> login", lan(xff="192.168.1.5", proto="https") == 303)
check("LAN: Forwarded proto=https -> login", lan(xff="192.168.1.5", fwd="for=1.2.3.4;proto=https") == 303)
check("LAN: XFF forjado ('privado, público') usa o último -> login", lan(xff="192.168.1.5, 8.8.8.8") == 303)
check("LAN: XFF 'público, privado' (último acrescentado pelo proxy) -> livre", lan(xff="8.8.8.8, 192.168.1.5") == 200)
check("LAN: par TCP público ignora o XFF forjado -> login", lan(peer="8.8.8.8", xff="192.168.1.5") == 303)
check("LAN: ligação direta de IP privado sem proxy -> livre", lan(peer="192.168.1.20") == 200)
settings.lan_trusted = False
check("LAN desligada (predefinição) -> login", lan(xff="192.168.1.5") == 303)

# --- sem Google configurado ---
settings.google_client_id = ""
cc = TestClient(m.app, base_url="http://x", client=("203.0.113.9", 1))
check("sem Google e sem LAN: / pede login (nunca aberto)", cc.get("/", headers=HTML, follow_redirects=False).status_code == 303)
check("sem Google: página de login avisa", "não está configurado" in cc.get("/auth/login").text)
check("sem Google: /auth/google -> 403", cc.get("/auth/google", follow_redirects=False).status_code == 403)
print(f"\n{ok_n} ok, {fail_n} falhas")
sys.exit(1 if fail_n else 0)

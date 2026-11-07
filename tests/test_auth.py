"""Testes da autenticação: palavra-passe, login OIDC (Auth0/Google simulado, id_token em JWT assinado com uma
chave RSA gerada aqui) e regra da rede local. Sem rede. Uso: python tests/test_auth.py"""
import base64
import os
import re
import sys
import time
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

import tempfile
os.environ["ENV_FILE"] = "/nonexistent/.env"  # isola os testes do .env real (WEB_PREFIX, OIDC_*, palavra-passe...)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")  # nunca toca na base real
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient

from app import auth, db, users
from app.config import settings
import app.main as m

ok_n = fail_n = 0
def check(name, cond):
    global ok_n, fail_n
    ok_n += bool(cond); fail_n += (not cond)
    print(("OK   " if cond else "FALHA"), name)

ISS = "https://tenant.test/"
settings.oidc_issuer, settings.oidc_client_id, settings.oidc_client_secret = "https://tenant.test", "cid", "sec"
settings.admin_emails = "ok@x.com"   # Admin de arranque (ADMIN_EMAILS)
settings.oidc_allowed_emails = ""
db.init()
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

def login(c, email=None):
    """Percorre /auth/sso -> callback; devolve a resposta do callback."""
    if email: idp.claims["email"] = email
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
check("HTML sem sessão -> 303 direto para /auth/sso (sem página/botão)", r.status_code == 303 and r.headers["location"].endswith("/auth/sso"))
r = c.get("/device/x/status", headers={"hx-request": "true"})
check("HTMX sem sessão -> 401 + HX-Redirect para /auth/sso", r.status_code == 401 and r.headers.get("hx-redirect", "").endswith("/auth/sso"))
check("fetch/JSON sem sessão -> 401", c.get("/device/x/codes").status_code == 401)
r = c.get("/auth/login", follow_redirects=False)
check("/auth/login sem mensagem -> 303 para /auth/sso (não mostra página)", r.status_code == 303 and r.headers["location"].endswith("/auth/sso"))
r = c.get("/auth/login?error=denied")
check("/auth/login com recusa mostra a mensagem e a ligação para tentar de novo (sem redirecionar em ciclo)",
      r.status_code == 200 and "foi recusado" in r.text and "Entrar de novo com SSO" in r.text)
idp.reset()
r = c.get("/auth/sso", follow_redirects=False)
loc = r.headers["location"]
check("início do fluxo -> /authorize com state, nonce e PKCE S256",
      loc.startswith("https://tenant.test/authorize?") and all(k in loc for k in ("state=", "nonce=", "code_challenge_method=S256")))
check("/auth/sso normal não força o pedido de credenciais", "prompt=" not in loc)
check("/auth/sso?prompt=login pede ao fornecedor as credenciais outra vez",
      "prompt=login" in c.get("/auth/sso?prompt=login", follow_redirects=False).headers["location"])
check("prompt desconhecido é ignorado", "prompt=" not in c.get("/auth/sso?prompt=none", follow_redirects=False).headers["location"])
check("a ligação 'Entrar de novo' usa prompt=login", "/auth/sso?prompt=login" in c.get("/auth/login?msg=bye").text)
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
check("logout leva à página 'Sessão terminada' (não volta a entrar sozinho)",
      r.headers["location"].endswith("/auth/login?msg=bye") and "Sessão terminada" in c.get("/auth/login?msg=bye").text)

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

# --- utilizadores, aprovação e papéis ---
ADMIN, JOE, ANA = "ok@x.com", "joe@x.com", "ana@x.com"
def uid(email): return users.by_email(email)["id"]
def act(admin_c, email, action): return admin_c.post(f"/admin/users/{uid(email)}/{action}", follow_redirects=False)

denied_with("email não verificado (inscrições abertas exigem verificação)", lambda: idp.claims.update(email_verified=False), "error=unverified")
check("email não verificado não cria utilizador", users.by_email("ok@x.com") is not None and all(u["email"] != "unv@x.com" for u in users.list_all()))
denied_with("sem claim de email", lambda: idp.claims.update(email=None), "error=denied")

idp.reset(); adm = client(); r, _ = login(adm, ADMIN)
check("ADMIN_EMAILS: 1.º login entra como Admin ativo", r.headers["location"] == "/" and users.by_email(ADMIN)["role"] == "admin" and users.by_email(ADMIN)["status"] == "active")
page = adm.get("/").text
check("Admin vê a ligação 'Utilizadores' e o papel Admin", "/admin/users" in page and ">Admin<" in page)
check("Admin acede a /admin/users", adm.get("/admin/users").status_code == 200)

idp.reset(); joe = client(); r, _ = login(joe, JOE)
check("novo utilizador -> pendente, sem acesso", r.headers["location"].endswith("/auth/login?msg=pending")
      and users.by_email(JOE)["status"] == "pending" and joe.get("/", headers=HTML, follow_redirects=False).status_code == 303)
check("página 'pendente' explica que aguarda aprovação", "aguarda a aprovação" in joe.get("/auth/login?msg=pending").text)
check("Admin vê o pedido pendente e o contador", JOE in adm.get("/admin/users").text and "pend" in adm.get("/").text)
r, _ = login(client(), JOE)
check("voltar a entrar enquanto pendente continua pendente", r.headers["location"].endswith("msg=pending"))
r = act(adm, JOE, "approve")
check("Admin aprova -> 303", r.status_code == 303 and "msg=ok" in r.headers["location"] and users.by_email(JOE)["status"] == "active")
joe = client(); r, _ = login(joe, JOE)
check("utilizador aprovado entra (papel user)", r.headers["location"] == "/" and joe.get("/").status_code == 200 and users.by_email(JOE)["role"] == "user")
page = joe.get("/").text
check("User não vê a ligação 'Utilizadores' e mostra o papel User", "/admin/users" not in page and ">User<" in page)
check("User não acede a /admin/users (403)", joe.get("/admin/users").status_code == 403)
check("User não pode agir sobre utilizadores (403)", joe.post(f"/admin/users/{uid(JOE)}/make_admin", follow_redirects=False).status_code == 403)
check("User não pode editar alarmes (403)", joe.post("/device/x/alarms", data={}).status_code == 403)
check("User pode controlar dispositivos (passa a autorização; dispositivo inexistente -> 404)", joe.post("/device/x/cmd", data={"code": "switch", "value": "true"}).status_code == 404)
check("Admin pode editar alarmes (passa a autorização; dispositivo inexistente -> 404)", adm.post("/device/x/alarms", data={}).status_code == 404)
# dispositivo de teste + estado simulado (sem rede) para ver a interface por papel
with db.connect() as cc:
    cc.execute("INSERT INTO devices(id,name,category,product_name,online,spec,updated_at) VALUES('dev1','Teste','dlq','x',1,NULL,0)")
async def fake_status(d): return [{"code": "alarm_set_1", "value": "BQEAZAcAAAA="}, {"code": "alarm_set_2", "value": "AQEAEAMBAQkEAQBa"}]
m.tuya.status = fake_status
ju, ad = joe.get("/device/dev1/alarms"), adm.get("/device/dev1/alarms")
check("User vê os alarmes só em leitura (sem formulário nem campos)", ju.status_code == 200 and "<form" not in ju.text and "<input" not in ju.text and "Só os administradores" in ju.text)
check("Admin vê os alarmes editáveis (formulário)", "<form" in ad.text and 'type="number"' in ad.text)
check("User não vê o botão Editar dos alarmes", 'class="edit-btn" data-target="#alarms"' not in joe.get("/device/dev1").text)
check("Admin vê o botão Editar dos alarmes", 'class="edit-btn" data-target="#alarms"' in adm.get("/device/dev1").text)

# promover e despromover
r = act(adm, JOE, "make_admin")
check("promover a Admin: acede a /admin/users", users.by_email(JOE)["role"] == "admin" and joe.get("/admin/users").status_code == 200)
act(adm, JOE, "make_user")
check("despromover a User: perde o acesso de Admin", joe.get("/admin/users").status_code == 403)

# desativar a meio da sessão, reativar, rejeitar
act(adm, JOE, "disable")
check("desativar: a sessão aberta deixa de valer (sem reiniciar)", joe.get("/", headers=HTML, follow_redirects=False).status_code == 303)
r, _ = login(client(), JOE)
check("desativado não entra (error=disabled)", "error=disabled" in r.headers["location"])
act(adm, JOE, "enable"); r, _ = login(client(), JOE)
check("reativar devolve o acesso", r.headers["location"] == "/")
r, _ = login(client(), ANA); check("2.º novo utilizador fica pendente", users.by_email(ANA)["status"] == "pending")
act(adm, ANA, "reject"); r, _ = login(client(), ANA)
check("rejeitado não entra (error=denied)", "error=denied" in r.headers["location"] and users.by_email(ANA)["status"] == "rejected")
act(adm, ANA, "approve"); r, _ = login(client(), ANA)
check("um rejeitado pode ser aprovado depois", r.headers["location"] == "/")

# proteções
r = act(adm, ADMIN, "disable")
check("Admin não se pode desativar a si próprio", "msg=proprio" in r.headers["location"] and users.by_email(ADMIN)["status"] == "active")
r = act(adm, ADMIN, "make_user")
check("Admin não se pode despromover a si próprio (nem se perde o último Admin)", "msg=proprio" in r.headers["location"] and users.active_admins() >= 1)
check("ação sobre estado inválido é ignorada", "msg=estado" in act(adm, ADMIN, "approve").headers["location"])
check("ação desconhecida -> 404", adm.post(f"/admin/users/{uid(ADMIN)}/apagar", follow_redirects=False).status_code == 404)
check("utilizador inexistente -> 404", adm.post("/admin/users/9999/approve", follow_redirects=False).status_code == 404)

# ADMIN_EMAILS promove um utilizador existente; OIDC_ALLOWED_EMAILS (descontinuado) conta como admin
settings.admin_emails = f"{ADMIN},{JOE}"
r, _ = login(client(), JOE)
check("email acrescentado a ADMIN_EMAILS passa a Admin ativo no login seguinte", users.by_email(JOE)["role"] == "admin")
settings.admin_emails = ADMIN; settings.oidc_allowed_emails = "legado@x.com"
r, _ = login(client(), "legado@x.com")
check("OIDC_ALLOWED_EMAILS (descontinuado) conta como ADMIN_EMAILS (sem perder acesso ao migrar)", users.by_email("legado@x.com")["role"] == "admin")
settings.oidc_allowed_emails = ""
settings.oidc_require_verified_email = False
idp.reset(); idp.claims.update(email_verified=False); c = client(); r, _ = login(c, "naover@x.com")
check("OIDC_REQUIRE_VERIFIED_EMAIL=false aceita email não verificado (fica pendente)", r.headers["location"].endswith("msg=pending"))
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
check("LAN confiável tem o papel User: não edita alarmes (403)",
      TestClient(m.app, base_url="http://lambdacore", client=("192.168.1.20", 1)).post("/device/x/alarms", data={}, headers={"host": "lambdacore"}).status_code == 403)
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
check("só palavra-passe: papel Admin (edita alarmes -> passa a autorização, 404)", c.post("/device/x/alarms", data={}, headers=basic("admin", "segredo")).status_code == 404)
settings.oidc_client_id = "cid"   # OIDC configurado: a palavra-passe deixa de valer
c = client()
check("com OIDC ativo: sem credenciais -> redireciona para o fornecedor", c.get("/", headers=HTML, follow_redirects=False).status_code == 303)
check("com OIDC ativo: HTTP Basic certo é ignorado (continua a pedir login)",
      c.get("/", headers={**HTML, **basic("admin", "segredo")}, follow_redirects=False).status_code == 303)
check("com OIDC ativo: fetch/JSON com HTTP Basic certo -> 401", c.get("/device/x/codes", headers=basic("admin", "segredo")).status_code == 401)
pg = c.get("/auth/login?error=denied").text
check("página de erro só oferece o fornecedor (sem palavra-passe)", "Entrar de novo com SSO" in pg and "palavra-passe" not in pg)
check("com OIDC ativo: /auth/basic não existe (404)", c.get("/auth/basic", headers=basic("admin", "segredo"), follow_redirects=False).status_code == 404)
settings.web_password = ""

print(f"\n{ok_n} ok, {fail_n} falhas")
sys.exit(1 if fail_n else 0)

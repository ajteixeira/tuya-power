"""Utilizadores e papéis (como no scrapper): inscrição no fornecedor (Auth0), autorização na app.

status: pending (a aguardar aprovação) | active | rejected | disabled      role: admin | user
"""
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db

templates = None  # definido por main.py
ROLES = ("admin", "user")


def _row(r):
    return dict(r) if r else None


def by_email(email: str):
    with db.connect() as c:
        return _row(c.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone())


def by_id(uid):
    with db.connect() as c:
        return _row(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def list_all():
    with db.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM users ORDER BY (status='pending') DESC, created_at DESC")]


def count_pending() -> int:
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0]


def active_admins() -> int:
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0]


def upsert_login(email: str, name: str, admin_emails: set[str]) -> dict:
    """Regista o login: os emails de ADMIN_EMAILS são sempre Admin ativos; os outros entram como «pendente»
    na 1.ª vez e mantêm o estado que o Admin lhes der."""
    email, now = email.lower(), int(time.time())
    boot = email in admin_emails
    with db.connect() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u:
            c.execute("INSERT INTO users(email,name,role,status,created_at,approved_at,approved_by) VALUES(?,?,?,?,?,?,?)",
                      (email, name, "admin" if boot else "user", "active" if boot else "pending", now,
                       now if boot else None, "ADMIN_EMAILS" if boot else None))
        elif boot and (u["role"] != "admin" or u["status"] != "active"):
            c.execute("UPDATE users SET role='admin', status='active', approved_at=?, approved_by='ADMIN_EMAILS' WHERE id=?",
                      (now, u["id"]))
        c.execute("UPDATE users SET name=COALESCE(NULLIF(?, ''), name), last_login=? WHERE email=?", (name, now, email))
        return dict(c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone())


def require_admin(request: Request):
    u = getattr(request.state, "user", None)
    if not u or u.get("role") != "admin":
        raise HTTPException(403, "Só os administradores podem fazer isto")


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

ACTIONS = {  # ação -> (estados de origem, novo estado, novo papel)
    "approve": ({"pending", "rejected"}, "active", None),
    "reject": ({"pending", "active"}, "rejected", None),
    "disable": ({"active"}, "disabled", None),
    "enable": ({"disabled", "rejected"}, "active", None),
    "make_admin": ({"active"}, None, "admin"),
    "make_user": ({"active"}, None, "user"),
}


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, msg: str = ""):
    return templates.TemplateResponse(request, "users.html", {"users": list_all(), "msg": msg,
                                                              "me": request.state.user.get("email")})


@router.post("/users/{uid}/{action}")
async def users_action(request: Request, uid: int, action: str):
    base = request.scope.get("tp_prefix", "") + "/admin/users"
    u, me = by_id(uid), request.state.user
    if action not in ACTIONS or not u:
        raise HTTPException(404)
    src, new_status, new_role = ACTIONS[action]
    if u["status"] not in src:
        return RedirectResponse(f"{base}?msg=estado", 303)
    # nunca se perde o último Admin nem se bloqueia o próprio
    if u["email"] == me.get("email") and action in ("reject", "disable", "make_user"):
        return RedirectResponse(f"{base}?msg=proprio", 303)
    if u["role"] == "admin" and u["status"] == "active" and (new_status or new_role == "user") and active_admins() <= 1:
        return RedirectResponse(f"{base}?msg=ultimo", 303)
    with db.connect() as c:
        if new_status:
            c.execute("UPDATE users SET status=?, approved_at=?, approved_by=? WHERE id=?",
                      (new_status, int(time.time()), me.get("email"), uid))
        if new_role:
            c.execute("UPDATE users SET role=? WHERE id=?", (new_role, uid))
    return RedirectResponse(f"{base}?msg=ok", 303)

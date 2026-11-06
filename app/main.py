import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import db
from .config import settings
from .sync import run_sync
from .tuya import TuyaError, tuya

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
security = HTTPBasic(auto_error=False)


def auth(cred: HTTPBasicCredentials = Depends(security)):
    if not settings.web_password:
        return
    ok = cred and secrets.compare_digest(cred.username, settings.web_user) \
        and secrets.compare_digest(cred.password, settings.web_password)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app):
    db.init()
    if settings.sync_in_app:
        scheduler.add_job(run_sync, "cron", hour=settings.sync_hour, minute=0, id="daily")
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown()


app = FastAPI(lifespan=lifespan, dependencies=[Depends(auth)])


EVENT_TYPES = {1: "Online", 2: "Offline", 3: "Ativação", 4: "Reposição", 5: "Comando enviado",
               6: "Atualização de firmware", 7: "Reporte de valores", 8: "Sinal do dispositivo",
               9: "Reinício", 10: "Temporização"}


def _dt(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["dt"] = _dt


def _spec(row):
    s = json.loads(row["spec"]) if row["spec"] else {}
    return s.get("functions") or [], s.get("status") or []


def _values(item):
    try:
        return json.loads(item.get("values") or "{}")
    except ValueError:
        return {}


def _scaled(code, value, spec_items):
    for it in spec_items:
        if it["code"] == code and isinstance(value, (int, float)) and not isinstance(value, bool):
            v = _values(it)
            val = value / 10 ** v.get("scale", 0)
            return f"{val:g} {v.get('unit', '')}".strip()
    return str(value)


def get_device(dev_id):
    with db.connect() as c:
        row = c.execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Dispositivo desconhecido (execute a sincronização).")
    return row


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    with db.connect() as c:
        devs = c.execute("SELECT * FROM devices ORDER BY name").fetchall()
        run = c.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    return templates.TemplateResponse(request, "index.html", {"devs": devs, "run": run})


@app.post("/sync", response_class=HTMLResponse)
async def sync_now():
    await run_sync()
    return HTMLResponse('<script>location="/"</script>')


@app.get("/device/{dev_id}", response_class=HTMLResponse)
async def device(request: Request, dev_id: str):
    d = get_device(dev_id)
    funcs, _ = _spec(d)
    with db.connect() as c:
        codes = [r[0] for r in c.execute(
            "SELECT DISTINCT code FROM logs WHERE device_id=? AND code IS NOT NULL ORDER BY code", (dev_id,))]
        types = [r[0] for r in c.execute(
            "SELECT DISTINCT event_type FROM logs WHERE device_id=? AND event_type IS NOT NULL ORDER BY 1", (dev_id,))]
    return templates.TemplateResponse(request, "device.html", {
        "d": d, "funcs": [dict(f, vals=_values(f)) for f in funcs],
        "log_codes": codes, "log_types": [(t, EVENT_TYPES.get(t, "Tipo %s" % t)) for t in types]})


@app.get("/device/{dev_id}/status", response_class=HTMLResponse)
async def status(request: Request, dev_id: str):
    d = get_device(dev_id)
    funcs, stat = _spec(d)
    try:
        st = await tuya.status(dev_id)
        err = None
    except TuyaError as e:
        st, err = [], str(e)
    items = [{"code": s["code"], "text": _scaled(s["code"], s["value"], stat + funcs)} for s in st]
    return templates.TemplateResponse(request, "_status.html", {"items": items, "err": err})


@app.post("/device/{dev_id}/cmd", response_class=HTMLResponse)
async def command(dev_id: str, code: str = Form(...), value: str = Form(""), kind: str = Form("String")):
    get_device(dev_id)
    if kind == "Boolean":
        v = value in ("true", "on", "1")
    elif kind == "Integer":
        v = int(float(value))
    else:
        v = value
    try:
        res = await tuya.send(dev_id, [{"code": code, "value": v}])
        return HTMLResponse(f'<span class="ok">{code} = {v} {"✔" if res else ""}</span>')
    except TuyaError as e:
        return HTMLResponse(f'<span class="err">Erro: {e}</span>', status_code=200)


@app.get("/device/{dev_id}/history")
async def history(dev_id: str, code: str, days: int = 7):
    d = get_device(dev_id)
    funcs, stat = _spec(d)
    scale = 0
    for it in stat + funcs:
        if it["code"] == code:
            scale = _values(it).get("scale", 0)
    since = int((time.time() - days * 86400) * 1000)
    with db.connect() as c:
        rows = c.execute("""SELECT event_time t, value v FROM logs WHERE device_id=? AND code=?
            AND event_time>=? ORDER BY event_time""", (dev_id, code, since)).fetchall()
    pts = []
    for r in rows:
        try:
            pts.append({"x": r["t"], "y": float(r["v"]) / 10 ** scale})
        except ValueError:
            pass
    return JSONResponse(pts)


@app.get("/device/{dev_id}/codes")
async def codes(dev_id: str):
    with db.connect() as c:
        rows = c.execute("SELECT DISTINCT code FROM logs WHERE device_id=? AND code IS NOT NULL ORDER BY code",
                         (dev_id,)).fetchall()
    return JSONResponse([r[0] for r in rows])


@app.get("/device/{dev_id}/log", response_class=HTMLResponse)
async def log(request: Request, dev_id: str, page: int = 0, start: str = "", end: str = "",
              vmin: str = "", vmax: str = "", types: list[int] = Query([]),
              codes: list[str] = Query([])):
    d = get_device(dev_id)
    funcs, stat = _spec(d)
    where, args = ["device_id=?"], [dev_id]
    for val, op in ((start, ">="), (end, "<=")):
        if val:
            try:
                ms = int(datetime.fromisoformat(val).timestamp() * 1000)
            except ValueError:
                continue
            where.append(f"event_time{op}?")
            args.append(ms + (59_999 if op == "<=" and len(val) <= 16 else 0))
    if types:
        where.append(f"event_type IN ({','.join('?' * len(types))})")
        args += types
    if codes:
        where.append(f"code IN ({','.join('?' * len(codes))})")
        args += codes
    # Mínimo/máximo aplicados ao valor já com escala (o que é mostrado); só valores numéricos
    scales = {it["code"]: _values(it).get("scale", 0) for it in stat + funcs
              if _values(it).get("scale")}
    scaled = "CAST(value AS REAL)"
    if scales:
        scaled += "/CASE code " + " ".join(f"WHEN ? THEN {10 ** v}" for v in scales.values()) + " ELSE 1 END"
    for val, op in ((vmin, ">="), (vmax, "<=")):
        try:
            num = float(val.replace(",", "."))
        except ValueError:
            continue
        where.append(f"(value GLOB '[0-9]*' OR value GLOB '-[0-9]*') AND {scaled}{op}?")
        args += list(scales) + [num]
    w = " AND ".join(where)
    with db.connect() as c:
        total = c.execute(f"SELECT COUNT(*) FROM logs WHERE {w}", args).fetchone()[0]
        rows = c.execute(f"SELECT * FROM logs WHERE {w} ORDER BY event_time DESC LIMIT 100 OFFSET ?",
                         args + [page * 100]).fetchall()
    out = [{"t": r["event_time"], "type": EVENT_TYPES.get(r["event_type"], r["event_type"]),
            "code": r["code"], "text": _scaled(r["code"], _num(r["value"]), stat + funcs)} for r in rows]
    return templates.TemplateResponse(request, "_log.html", {
        "rows": out, "total": total, "page": page, "more": (page + 1) * 100 < total, "dev_id": dev_id})


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v

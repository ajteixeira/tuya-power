import asyncio
import base64
import binascii
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


# Registos dos alarm_set_* de um disjuntor: [tipo, corte ativo, valor uint16 BE].
# Tipos 1, 3, 4, 5 e 7 confirmados com a app Tuya Smart (alarmes do disjuntor); 2 e 6 desconhecidos.
# Unidade/escala do tipo 7 (potência alta) por confirmar: na app aparece em kW.
ALARM_TYPES = {1: ("Sobrecorrente", "A"), 3: ("Sobretensão", "V"), 4: ("Subtensão", "V"),
               5: ("Temperatura alta", "°C"), 7: ("Potência alta", "kW")}


def _raw(code, value):
    """Tipo Raw: a Tuya envia base64. Mostra os bytes em hexadecimal; os alarm_set_* são
    descodificados em registos de 4 bytes (formato não documentado pela Tuya, validado com a app)."""
    try:
        data = base64.b64decode(str(value), validate=True)
    except (binascii.Error, ValueError):
        return str(value)
    if not data:
        return str(value)
    txt = f"{data.hex(' ')}  ({value})"
    if code in ("phase_a", "phase_b", "phase_c") and len(data) == 8:
        # Formato padrão Tuya: tensão 0,1 V (2 B) · corrente mA (3 B) · potência W (3 B)
        v, i, w = int.from_bytes(data[0:2], "big") / 10, int.from_bytes(data[2:5], "big"), int.from_bytes(data[5:8], "big")
        return f"{v:g} V · {i / 1000:g} A · {w:g} W  [{txt}]"
    if code.startswith("alarm_set") and len(data) % 4 == 0:
        recs = []
        for i in range(0, len(data), 4):
            t, on, val = data[i], data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
            name, unit = ALARM_TYPES.get(t, (f"tipo {t}", ""))
            recs.append(f"{name}: {val} {unit}".rstrip() + f" · corte {'ativo' if on else 'inativo'}")
        return " | ".join(recs) + f"  [{txt}]"
    return txt


def _parse_alarm(value):
    """Registos [tipo, corte, valor uint16 BE] de um alarm_set_*; None se o formato não encaixar."""
    try:
        data = base64.b64decode(str(value), validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) % 4:
        return None
    recs = []
    for i in range(0, len(data), 4):
        name, unit = ALARM_TYPES.get(data[i], (f"Tipo {data[i]}", ""))
        recs.append({"t": data[i], "on": bool(data[i + 1]), "val": int.from_bytes(data[i + 2:i + 4], "big"),
                     "name": name, "unit": unit})
    return recs


def _build_alarm(recs):
    return base64.b64encode(b"".join(
        bytes([r["t"], 1 if r["on"] else 0]) + r["val"].to_bytes(2, "big") for r in recs)).decode()


async def _alarm_forms(request, dev_id, msg="", err=False):
    try:
        st = await tuya.status(dev_id)
        e = None
    except TuyaError as ex:
        st, e = [], str(ex)
    alarms = [{"code": x["code"], "recs": r} for x in st
              if x["code"].startswith("alarm_set") and (r := _parse_alarm(x["value"]))]
    raws = []
    for x in st:
        if x["code"].startswith("alarm_set"):
            try:
                hx = base64.b64decode(str(x["value"]), validate=True).hex(" ")
            except (binascii.Error, ValueError):
                hx = ""
            raws.append({"code": x["code"], "b64": x["value"], "hex": hx})
    return templates.TemplateResponse(request, "_alarms.html", {
        "alarms": alarms, "raws": raws, "dev_id": dev_id, "msg": msg or e or "", "err": err or bool(e)})


@app.get("/device/{dev_id}/alarms", response_class=HTMLResponse)
async def alarms(request: Request, dev_id: str):
    get_device(dev_id)
    return await _alarm_forms(request, dev_id)


@app.post("/device/{dev_id}/alarms", response_class=HTMLResponse)
async def alarms_set(request: Request, dev_id: str):
    """Recebe a tabela inteira (campos order__<código>, v__<código>__<tipo>, on__<código>__<tipo>)
    e só envia os alarm_set_* cujo valor mudou."""
    get_device(dev_id)
    form = await request.form()
    try:
        current = {x["code"]: x["value"] for x in await tuya.status(dev_id)}
        cmds = []
        for code in dict.fromkeys(k.split("__", 1)[1] for k in form if k.startswith("order__")):
            if not code.startswith("alarm_set"):
                raise ValueError("código inválido")
            recs = []
            for t in form.getlist(f"order__{code}"):
                val = int(float(str(form.get(f"v__{code}__{t}", "0")).replace(",", ".")))
                if not 0 <= val <= 65535:
                    raise ValueError(f"valor fora do intervalo (0–65535) em {code}, tipo {t}")
                recs.append({"t": int(t), "on": f"on__{code}__{t}" in form, "val": val})
            value = _build_alarm(recs)
            if value != current.get(code):
                cmds.append({"code": code, "value": value})
        if not cmds:
            return await _alarm_forms(request, dev_id, "Sem alterações.")
        await tuya.send(dev_id, cmds)
    except (ValueError, TuyaError) as e:
        return await _alarm_forms(request, dev_id, f"Erro: {e}", err=True)
    await asyncio.sleep(1.5)  # dar tempo ao dispositivo para reportar o novo valor
    return await _alarm_forms(request, dev_id, "Enviado: " + ", ".join(c["code"] for c in cmds) + ".")


# A Tuya não declara a unidade de alguns códigos (add_ele é em kW·h, escala 3)
DEFAULT_UNITS = {"add_ele": "kW·h"}


def _scaled(code, value, spec_items):
    for it in spec_items:
        if it["code"] == code and it.get("type") == "Raw":
            return _raw(code, value)
    for it in spec_items:
        if it["code"] == code and isinstance(value, (int, float)) and not isinstance(value, bool):
            v = _values(it)
            val = value / 10 ** v.get("scale", 0)
            return f"{val:g} {v.get('unit') or DEFAULT_UNITS.get(code, '')}".strip()
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


LABELS = {
    "switch": "Interruptor", "switch_1": "Interruptor", "cur_power": "Potência", "cur_voltage": "Tensão",
    "cur_current": "Corrente", "add_ele": "Energia acumulada", "total_forward_energy": "Energia total consumida",
    "reverse_energy_total": "Energia total injetada", "leakage_current": "Corrente de fuga",
    "temp_value": "Temperatura", "temp_current": "Temperatura", "va_temperature": "Temperatura",
    "va_humidity": "Humidade", "battery_percentage": "Bateria", "balance_energy": "Energia (saldo)",
    "countdown_1": "Contagem decrescente", "fault": "Falhas", "relay_status": "Estado ao ligar",
    "light_mode": "Luz indicadora", "child_lock": "Bloqueio infantil", "cycle_time": "Ciclos",
    "random_time": "Aleatório", "overcharge_switch": "Corte por sobrecarga",
    "phase_a": "Fase A", "phase_b": "Fase B", "phase_c": "Fase C", "switch_prepayment": "Pré-pagamento", "charge_energy": "Energia carregada",
}
MAIN = ["switch", "switch_1", "phase_a", "phase_b", "phase_c", "cur_power", "cur_voltage", "cur_current", "add_ele", "total_forward_energy",
        "reverse_energy_total", "leakage_current", "temp_value", "temp_current", "va_temperature",
        "va_humidity", "battery_percentage", "balance_energy"]


@app.get("/device/{dev_id}/status", response_class=HTMLResponse)
async def status(request: Request, dev_id: str):
    d = get_device(dev_id)
    funcs, stat = _spec(d)
    try:
        st = await tuya.status(dev_id)
        err = None
    except TuyaError as e:
        st, err = [], str(e)
    main, other = [], []
    for x in st:
        code, val = x["code"], x["value"]
        label = LABELS.get(code, code)
        if code.startswith("alarm_set") and _parse_alarm(val):
            continue  # mostrados no separador «Alarmes»
        item = {"code": code, "label": label, "text": _scaled(code, val, stat + funcs),
                "bool": val if isinstance(val, bool) else None, "is_switch": code.startswith("switch")}
        if code in MAIN:
            item["text"] = item["text"].split("  [", 1)[0] or "—"
        elif not item["text"]:
            item["text"] = "—"
        (main if code in MAIN else other).append(item)
    main.sort(key=lambda i: MAIN.index(i["code"]))
    return templates.TemplateResponse(request, "_status.html", {
        "main": main, "other": other, "err": err,
        "now": datetime.now().strftime("%H:%M:%S")})


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

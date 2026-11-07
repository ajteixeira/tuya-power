import asyncio
import base64
import binascii
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from markupsafe import Markup
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from starlette.middleware.sessions import SessionMiddleware

from . import auth, db, users
from .config import settings
from .sync import run_sync
from .tuya import TuyaError, tuya

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app):
    db.init()
    if settings.sync_in_app:
        if settings.sync_interval_minutes > 0:  # por ex. de hora a hora, com a 1.ª execução logo ao arrancar
            scheduler.add_job(run_sync, "interval", minutes=settings.sync_interval_minutes, id="sync",
                              next_run_time=datetime.now())
        else:
            scheduler.add_job(run_sync, "cron", hour=settings.sync_hour, minute=0, id="daily")
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown()


class ForwardedPrefixMiddleware:
    """Atrás do lighttpd (/power): retira o prefixo do cabeçalho X-Forwarded-Prefix do caminho e guarda-o
    no scope, para as ligações geradas nas páginas o incluírem (como nas outras aplicações /ytd e /sc)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            prefix = dict(scope["headers"]).get(b"x-forwarded-prefix", b"").decode("latin-1").rstrip("/")
            scope = dict(scope, tp_prefix=prefix)
            path = scope["path"]
            if prefix and (path == prefix or path.startswith(prefix + "/")):
                path = path[len(prefix):] or "/"
                scope["path"], scope["raw_path"] = path, path.encode("utf-8")
        await self.app(scope, receive, send)


app = FastAPI(lifespan=lifespan, dependencies=[Depends(auth.require_login)])
app.include_router(auth.router)
app.include_router(users.router)
# Sessão (cookie próprio, no caminho do prefixo, para não colidir com /sc e /ytd); o prefixo trata-se primeiro
app.add_middleware(SessionMiddleware, secret_key=auth.session_secret(), session_cookie="tuya_power_session",
                   path=settings.web_prefix or "/", max_age=7 * 86400, same_site="lax",
                   https_only=settings.auth_redirect_uri.startswith("https://"))
app.add_middleware(ForwardedPrefixMiddleware)


EVENT_TYPES = {1: "Online", 2: "Offline", 3: "Ativação", 4: "Reposição", 5: "Comando enviado",
               6: "Atualização de firmware", 7: "Reporte de valores", 8: "Sinal do dispositivo",
               9: "Reinício", 10: "Temporização"}


def _dt(ms, fmt="datetime"):
    """Data/hora como <time data-ms>: o navegador reescreve-a no formato e fuso das definições
    do sistema; o texto do servidor serve só de alternativa (sem JavaScript)."""
    d = datetime.fromtimestamp(ms / 1000)
    txt = {"time": d.strftime("%H:%M:%S"), "hm": d.strftime("%H:%M")}.get(fmt) or d.strftime("%Y-%m-%d %H:%M:%S")
    return Markup(f'<time data-ms="{int(ms)}" data-fmt="{fmt}">{txt}</time>')


templates.env.filters["dt"] = _dt
auth.templates = templates
users.templates = templates
templates.env.globals["pending_users"] = users.count_pending
templates.env.globals["P"] = lambda request: request.scope.get("tp_prefix", "")  # prefixo do proxy (ex.: /power)


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
        "alarms": alarms, "raws": raws, "can_edit": getattr(request.state, "user", {}).get("role") == "admin", "dev_id": dev_id, "msg": msg or e or "", "err": err or bool(e)})


@app.get("/device/{dev_id}/alarms", response_class=HTMLResponse)
async def alarms(request: Request, dev_id: str):
    get_device(dev_id)
    return await _alarm_forms(request, dev_id)


@app.post("/device/{dev_id}/alarms", response_class=HTMLResponse, dependencies=[Depends(users.require_admin)])
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
    return HTMLResponse("<script>location.reload()</script>")


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
    "cur_current": "Corrente", "add_ele": "Energia (último incremento)", "total_forward_energy": "Energia total consumida",
    "reverse_energy_total": "Energia total injetada", "leakage_current": "Corrente de fuga",
    "temp_value": "Temperatura", "temp_current": "Temperatura", "va_temperature": "Temperatura",
    "va_humidity": "Humidade", "battery_percentage": "Bateria", "balance_energy": "Energia (saldo)",
    "countdown_1": "Contagem decrescente", "fault": "Falhas", "relay_status": "Estado ao ligar",
    "light_mode": "Luz indicadora", "child_lock": "Bloqueio infantil", "cycle_time": "Ciclos",
    "random_time": "Aleatório", "overcharge_switch": "Corte por sobrecarga",
    "humidity_value": "Humidade", "battery_state": "Bateria", "temp_unit_convert": "Unidade de temperatura",
    "phase_a": "Fase A", "phase_b": "Fase B", "phase_c": "Fase C", "switch_prepayment": "Pré-pagamento", "charge_energy": "Energia carregada",
}
MAIN = ["switch", "switch_1", "phase_a", "phase_b", "phase_c", "cur_power", "cur_voltage", "cur_current", "add_ele", "total_forward_energy",
        "reverse_energy_total", "leakage_current", "temp_value", "temp_current", "va_temperature",
        "va_humidity", "humidity_value", "battery_percentage", "battery_state", "today_forward", "today_reverse"]


@app.get("/device/{dev_id}/status", response_class=HTMLResponse)
async def status(request: Request, dev_id: str, since: int | None = None):
    """since: início do dia no fuso do navegador (ms), para o consumo de hoje."""
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
        if code == "add_ele":
            # cada valor de add_ele é a energia consumida desde o reporte anterior: o consumo de hoje é a soma
            day0 = since if since is not None else int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
            scale = next((_values(i).get("scale", 0) for i in stat + funcs if i["code"] == code), 0)
            with db.connect() as c:
                tot, last = c.execute("""SELECT SUM(CAST(value AS REAL)), MAX(event_time) FROM logs
                    WHERE device_id=? AND code='add_ele' AND event_time>=?""", (dev_id, day0)).fetchone()
            tot = (tot or 0) / 10 ** scale
            lbl = Markup("Energia consumida hoje") if not last else Markup("Energia consumida hoje (até ") + _dt(last, "hm") + Markup(")")
            main.append({"code": code, "label": lbl, "text": f"{round(tot, 2):g} {DEFAULT_UNITS['add_ele']}",
                         "bool": None, "is_switch": False})
            continue
        item = {"code": code, "label": label, "text": _scaled(code, val, stat + funcs),
                "bool": val if isinstance(val, bool) else None, "is_switch": code.startswith("switch")}
        if code in MAIN:
            item["text"] = item["text"].split("  [", 1)[0] or "—"
        elif not item["text"]:
            item["text"] = "—"
        (main if code in MAIN else other).append(item)
    # Contadores acumulados (Geral): consumo/injeção de hoje = valor atual menos o último valor antes das 00:00
    live = {x["code"]: x["value"] for x in st}
    day0 = since if since is not None else int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    for code, tag, lbl in (("total_forward_energy", "today_forward", "Energia consumida hoje"),
                           ("reverse_energy_total", "today_reverse", "Energia injetada hoje")):
        val = live.get(code)
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        spec = next((_values(i) for i in stat + funcs if i["code"] == code), {})
        num = "(value GLOB '[0-9]*' OR value GLOB '-[0-9]*') AND value NOT GLOB '*[^0-9.eE+-]*'"
        with db.connect() as c:
            base = c.execute(f"""SELECT value FROM logs WHERE device_id=? AND code=? AND event_time<? AND {num}
                ORDER BY event_time DESC LIMIT 1""", (dev_id, code, day0)).fetchone()
            if base is None:  # sem dados antes de hoje: usar a primeira leitura de hoje
                base = c.execute(f"""SELECT value FROM logs WHERE device_id=? AND code=? AND event_time>=? AND {num}
                    ORDER BY event_time LIMIT 1""", (dev_id, code, day0)).fetchone()
        if base is None:
            continue
        delta = max(val - float(base[0]), 0) / 10 ** spec.get("scale", 0)
        main.append({"code": tag, "label": lbl, "text": f"{round(delta, 2):g} {spec.get('unit', 'kW·h')}",
                     "bool": None, "is_switch": False})
    main.sort(key=lambda i: MAIN.index(i["code"]))
    return templates.TemplateResponse(request, "_status.html", {
        "main": main, "other": other, "err": err,
        "now_ms": int(time.time() * 1000)})


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
async def history(dev_id: str, code: str, since: int, until: int | None = None):
    """Pontos do intervalo [since, until) (ms, calculados pelo navegador no seu fuso) e o último
    ponto anterior ao intervalo («before»), necessário para o consumo do primeiro período."""
    d = get_device(dev_id)
    funcs, stat = _spec(d)
    scale, unit = 0, ""
    for it in stat + funcs:
        if it["code"] == code:
            v = _values(it)
            scale, unit = v.get("scale", 0), v.get("unit") or DEFAULT_UNITS.get(code, "")
    until = until if until is not None else int(time.time() * 1000) + 1
    num = "(value GLOB '[0-9]*' OR value GLOB '-[0-9]*') AND value NOT GLOB '*[^0-9.eE+-]*'"
    with db.connect() as c:
        rows = c.execute(f"""SELECT event_time t, value v FROM logs WHERE device_id=? AND code=?
            AND event_time>=? AND event_time<? AND {num} ORDER BY event_time""",
                         (dev_id, code, since, until)).fetchall()
        prev = c.execute(f"""SELECT event_time t, value v FROM logs WHERE device_id=? AND code=?
            AND event_time<? AND {num} ORDER BY event_time DESC LIMIT 1""", (dev_id, code, since)).fetchone()
    pts = [{"x": r["t"], "y": float(r["v"]) / 10 ** scale} for r in rows]
    before = {"x": prev["t"], "y": float(prev["v"]) / 10 ** scale} if prev else None
    return JSONResponse({"points": pts, "before": before, "unit": unit, "label": LABELS.get(code, code)})


# Energia por período (hora, dia ou mês). Há dois tipos de código:
#  - contadores acumulados (total_forward_energy, reverse_energy_total): consumo = último valor do
#    período menos o do período anterior;
#  - incrementos (add_ele, «electricity added»): cada reporte é a energia consumida desde o anterior,
#    logo o consumo do período é a SOMA dos valores (confirmado: bate com o integral de cur_power).
ENERGY_RAW = {"total_forward_energy", "reverse_energy_total", "add_ele"}  # só se oferecem «por período»
CUMULATIVE = {"total_forward_energy": "Energia consumida por período",
              "reverse_energy_total": "Energia injetada por período",
              "add_ele": "Energia consumida por período"}


@app.get("/device/{dev_id}/codes")
async def codes(dev_id: str):
    """Códigos com valores numéricos (os únicos que se podem desenhar no gráfico), com a etiqueta
    a mostrar; os códigos de energia só se oferecem por período («:delta»), não os valores brutos."""
    with db.connect() as c:
        rows = c.execute("""SELECT DISTINCT code FROM logs WHERE device_id=? AND code IS NOT NULL
            AND (value GLOB '[0-9]*' OR value GLOB '-[0-9]*') AND value NOT GLOB '*[^0-9.eE+-]*'
            ORDER BY code""", (dev_id,)).fetchall()
    out = []
    for (code,) in rows:
        if code not in ENERGY_RAW:  # totais/incrementos de energia não se oferecem como gráfico
            out.append({"id": code, "label": LABELS.get(code, code)})
        if code in CUMULATIVE:
            agg = "sum" if code == "add_ele" else "counter"
            out.append({"id": code + ":delta", "label": CUMULATIVE[code], "agg": agg})
    return JSONResponse(out)


@app.get("/device/{dev_id}/log", response_class=HTMLResponse)
async def log(request: Request, dev_id: str, page: int = 0, start: str = "", end: str = "",
              vmin: str = "", vmax: str = "", types: list[int] = Query([]),
              codes: list[str] = Query([])):
    d = get_device(dev_id)
    funcs, stat = _spec(d)
    where, args = ["device_id=?"], [dev_id]
    for val, op in ((start, ">="), (end, "<=")):
        if val:
            try:  # o navegador envia milissegundos (no seu fuso); texto ISO = hora local do servidor
                ms = int(val) if val.isdigit() else int(datetime.fromisoformat(val).timestamp() * 1000)
            except ValueError:
                continue
            where.append(f"event_time{op}?")
            args.append(ms + (59_999 if op == "<=" and not val.isdigit() and len(val) <= 16 else 0))
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

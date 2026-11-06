"""Descarga incremental e sem duplicados do histórico (UNIQUE + INSERT OR IGNORE)."""
import asyncio
import json
import time

from . import db
from .config import settings
from .tuya import TuyaError, tuya

_lock = asyncio.Lock()


async def sync_devices():
    devs = await tuya.list_devices()
    now = int(time.time())
    with db.connect() as c:
        for d in devs:
            try:
                spec = json.dumps(await tuya.specifications(d["id"]))
            except TuyaError:
                spec = None
            c.execute("""INSERT INTO devices(id,name,category,product_name,model,online,icon,spec,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
              category=excluded.category, product_name=excluded.product_name, model=excluded.model,
              online=excluded.online, icon=excluded.icon, spec=COALESCE(excluded.spec,spec),
              updated_at=excluded.updated_at""",
                      (d["id"], d.get("name"), d.get("category"), d.get("product_name"),
                       d.get("model"), int(bool(d.get("online"))), d.get("icon"), spec, now))
    return [d["id"] for d in devs]


async def sync_device_logs(dev_id):
    end = int(time.time() * 1000)
    with db.connect() as c:
        r = c.execute("SELECT last_event_time FROM sync_state WHERE device_id=?", (dev_id,)).fetchone()
    # Sobreposição de 1 h: a UNIQUE descarta repetidos
    start = (r[0] - 3600_000) if r else end - settings.sync_initial_days * 86400_000
    new, key, last = 0, "", r[0] if r else 0
    while True:
        res = await tuya.logs(dev_id, start, end, key)
        rows = res.get("logs") or []
        with db.connect() as c:
            for l in rows:
                cur = c.execute("""INSERT OR IGNORE INTO logs(device_id,event_time,event_type,code,value,event_from)
                    VALUES(?,?,?,?,?,?)""", (dev_id, l["event_time"], l.get("event_id"), l.get("code"),
                                             str(l.get("value")), l.get("event_from")))
                new += cur.rowcount
                last = max(last, l["event_time"])
        if not res.get("has_next") or not rows:
            break
        key = res.get("next_row_key", "")
        await asyncio.sleep(0.5)
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO sync_state VALUES(?,?)", (dev_id, last or end))
    return new


async def run_sync():
    if _lock.locked():
        return
    async with _lock:
        started, total, errs = int(time.time()), 0, []
        try:
            dev_ids = await sync_devices()
        except Exception as e:  # registar e continuar o agendador
            dev_ids, errs = [], [str(e)]
        for dev_id in dev_ids:  # um dispositivo com erro não bloqueia os outros
            try:
                total += await sync_device_logs(dev_id)
            except Exception as e:
                errs.append(f"{dev_id}: {e}")
        err = "; ".join(errs) or None
        with db.connect() as c:
            c.execute("INSERT INTO sync_runs(started,finished,new_rows,error) VALUES(?,?,?,?)",
                      (started, int(time.time()), total, err))

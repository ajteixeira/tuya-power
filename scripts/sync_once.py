"""Executa uma sincronização completa (útil para depurar app/sync.py e app/tuya.py)."""
import asyncio

from app import db
from app.sync import run_sync

db.init()
asyncio.run(run_sync())
with db.connect() as c:
    print(dict(c.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()))

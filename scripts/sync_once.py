"""Executa uma sincronização completa (útil para depurar app/sync.py e app/tuya.py)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.sync import run_sync

db.init()
asyncio.run(run_sync())
with db.connect() as c:
    run = dict(c.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone())
print(run)
sys.exit(1 if run["error"] else 0)

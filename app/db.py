import sqlite3
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
  id TEXT PRIMARY KEY, name TEXT, category TEXT, product_name TEXT, model TEXT,
  online INTEGER, icon TEXT, spec TEXT, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, event_time INTEGER NOT NULL,
  event_type INTEGER, code TEXT, value TEXT, event_from TEXT,
  UNIQUE(device_id, event_time, event_type, code, value));
CREATE INDEX IF NOT EXISTS ix_logs_dev ON logs(device_id, code, event_time);
CREATE TABLE IF NOT EXISTS sync_runs(
  id INTEGER PRIMARY KEY, started INTEGER, finished INTEGER, new_rows INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS sync_state(device_id TEXT PRIMARY KEY, last_event_time INTEGER);
"""


def connect():
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.db_path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init():
    with connect() as c:
        c.executescript(SCHEMA)

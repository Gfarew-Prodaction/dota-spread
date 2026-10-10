import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS snapshots (key TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, fallback=None):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM snapshots WHERE key = ?', (key,)).fetchone()
        return json.loads(row[0]) if row else fallback

    def put(self, key, value):
        with self.connect() as db:
            db.execute('INSERT INTO snapshots VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload', (key, json.dumps(value, ensure_ascii=False)))

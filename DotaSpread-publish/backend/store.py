"""Shared snapshots and fenced collector leases, on PostgreSQL or local SQLite."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path=None, database_url=None):
        self.path = path
        self.backend = 'postgresql' if database_url else 'sqlite'
        self.persistent = bool(database_url)
        self.pool = None
        if database_url:
            from psycopg_pool import ConnectionPool
            if not database_url.startswith(('postgres://', 'postgresql://')):
                raise ValueError('DATABASE_URL должна быть строкой подключения PostgreSQL.')
            self.pool = ConnectionPool(
                database_url, min_size=0, max_size=4, timeout=10, max_idle=60,
                kwargs={'connect_timeout': 8, 'prepare_threshold': None},
                check=ConnectionPool.check_connection, open=True,
            )
        else:
            if not path:
                raise ValueError('Укажите путь SQLite или DATABASE_URL.')
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connect(write=True) as db:
                self.execute(db, 'CREATE TABLE IF NOT EXISTS snapshots (key TEXT PRIMARY KEY, payload TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 0)')
                self.execute(db, '''CREATE TABLE IF NOT EXISTS steam_jobs (
                    name TEXT PRIMARY KEY, quote_payload TEXT,
                    checked_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                    attempted_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                    lease_token TEXT, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
                    change_seq BIGINT NOT NULL DEFAULT 0
                )''')
                self.execute(db, 'CREATE TABLE IF NOT EXISTS source_control (source TEXT PRIMARY KEY, payload TEXT NOT NULL)')
                self._ensure_column(db, 'snapshots', 'revision')
                self._ensure_column(db, 'steam_jobs', 'change_seq')
                self.execute(db, 'CREATE INDEX IF NOT EXISTS steam_jobs_changes ON steam_jobs (change_seq)')
                self._migrate(db)
        except Exception:
            self.close()
            raise

    @contextmanager
    def connect(self, write=False):
        if self.pool:
            with self.pool.connection() as db:
                yield db
            return
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                if write:
                    db.execute('BEGIN IMMEDIATE')
                yield db
        finally:
            db.close()

    def execute(self, db, sql, params=()):
        return db.execute(sql.replace('?', '%s') if self.pool else sql, params)

    def close(self):
        if self.pool:
            self.pool.close()

    def _read(self, db, key, fallback=None):
        row = self.execute(db, 'SELECT payload FROM snapshots WHERE key = ?', (key,)).fetchone()
        return json.loads(row[0]) if row else fallback

    def _put(self, db, key, value):
        self.execute(db, '''INSERT INTO snapshots (key, payload, revision) VALUES (?, ?, 1)
            ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, revision=snapshots.revision + 1''',
            (key, json.dumps(value, ensure_ascii=False)))

    def _ensure_column(self, db, table, column):
        if self.pool:
            self.execute(db, f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} BIGINT NOT NULL DEFAULT 0')
        elif column not in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {column} BIGINT NOT NULL DEFAULT 0')

    def _locked_control(self, db, source):
        self.execute(db, '''INSERT INTO source_control (source, payload) VALUES (?, ?)
            ON CONFLICT(source) DO NOTHING''', (source, '{}'))
        suffix = ' FOR UPDATE' if self.pool else ''
        row = self.execute(db, 'SELECT payload FROM source_control WHERE source = ?' + suffix, (source,)).fetchone()
        return json.loads(row[0])

    def _write_control(self, db, source, state):
        self.execute(db, 'UPDATE source_control SET payload = ? WHERE source = ?',
                     (json.dumps(state, ensure_ascii=False), source))

    def _migrate(self, db):
        # The Steam control row serializes migration across collector processes.
        self._locked_control(db, 'steam')
        if self._read(db, 'storage_version') in (2, 3):
            if self._read(db, 'storage_version') == 2:
                self._put(db, 'storage_version', 3)
            return
        quotes = self._read(db, 'quotes', {})
        old = self._read(db, 'collector_state', {})
        checked = set(old.get('checked_names', [])) | set(quotes)
        attempted = old.get('attempted', {})
        for name in checked | set(attempted):
            data = quotes.get(name)
            stamp = data.get('checked_at', 0) if data else (attempted.get(name, 0) or 1)
            self.execute(db, '''INSERT INTO steam_jobs (name, quote_payload, checked_at, attempted_at)
                VALUES (?, ?, ?, ?) ON CONFLICT(name) DO NOTHING''',
                (name, json.dumps(data, ensure_ascii=False) if data else None,
                 stamp if name in checked else 0, attempted.get(name, 0)))
        for source in ('lis', 'steam', 'fx'):
            state = self._locked_control(db, source)
            state.update(retry_at=old.get('retry_at', {}).get(source, 0),
                         failures=old.get('failures', {}).get(source, 0))
            if source == 'steam':
                state.update(request_interval=old.get('request_interval', 4), scan_turn=old.get('scan_turn', 0))
            self._write_control(db, source, state)
        self._put(db, 'storage_version', 3)
        self.execute(db, "DELETE FROM snapshots WHERE key IN ('quotes', 'collector_state')")

    def get(self, key, fallback=None):
        if key in ('quotes', 'collector_state'):
            return self.read_market()[key]
        with self.connect() as db:
            return self._read(db, key, fallback)

    def put(self, key, value):
        self.put_many({key: value})

    def put_many(self, values):
        with self.connect(write=True) as db:
            for key, value in values.items():
                if key == 'quotes':
                    # Compatibility for importing a previous local cache.
                    state = self._locked_control(db, 'steam')
                    seq = state['revision'] = state.get('revision', 0) + 1
                    self._write_control(db, 'steam', state)
                    self.execute(db, 'UPDATE steam_jobs SET quote_payload = NULL, change_seq = ?', (seq,))
                    for name, data in value.items():
                        self.execute(db, '''INSERT INTO steam_jobs (name, quote_payload, checked_at, change_seq)
                            VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET
                            quote_payload=excluded.quote_payload, checked_at=excluded.checked_at,
                            change_seq=excluded.change_seq''',
                            (name, json.dumps(data, ensure_ascii=False), data['checked_at'], seq))
                else:
                    self._put(db, key, value)

    def read_market(self, known_versions=None, since_revision=None):
        with self.connect() as db:
            # Read the revision before data, so concurrent commits can only cause a
            # harmless repeated delta, never a missed quote or invalidation.
            controls = {source: json.loads(payload) for source, payload in
                        self.execute(db, 'SELECT source, payload FROM source_control').fetchall()}
            versions = dict(self.execute(db, 'SELECT key, revision FROM snapshots').fetchall())
            changed = [key for key, version in versions.items()
                       if known_versions is None or known_versions.get(key) != version]
            values = {}
            if changed:
                placeholders = ','.join('?' for _ in changed)
                values = {key: json.loads(payload) for key, payload in self.execute(
                    db, f'SELECT key, payload FROM snapshots WHERE key IN ({placeholders})', changed).fetchall()}
            sql = 'SELECT name, quote_payload, checked_at, attempted_at FROM steam_jobs'
            jobs = self.execute(db, sql + (' WHERE change_seq > ?' if since_revision is not None else ''),
                                (since_revision,) if since_revision is not None else ()).fetchall()
        values['quotes'] = {name: json.loads(payload) if payload else None for name, payload, _, _ in jobs}
        if since_revision is None:
            values['quotes'] = {name: data for name, data in values['quotes'].items() if data is not None}
        steam = controls.get('steam', {})
        values['collector_state'] = {
            'attempted': {name: attempted for name, _, _, attempted in jobs if attempted},
            'checked_names': [name for name, _, checked, _ in jobs if checked],
            'scan_turn': steam.get('scan_turn', 0),
            'retry_at': {source: state.get('retry_at', 0) for source, state in controls.items()},
            'failures': {source: state.get('failures', 0) for source, state in controls.items()},
            'request_interval': steam.get('request_interval', 4),
        }
        values['controls'] = controls
        values['snapshot_versions'] = versions
        values['steam_revision'] = steam.get('revision', 0)
        values['incremental'] = since_revision is not None
        return values

    def mutate_control(self, source, update):
        with self.connect(write=True) as db:
            state = update(self._locked_control(db, source))
            self._write_control(db, source, state)
        return state

    def claim(self, source, worker, candidates=None, now=None, lease_seconds=180, request_interval=4):
        """Reserve a source and, for Steam, one item. No network I/O in this transaction."""
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            state = self._locked_control(db, source)
            state['request_interval'] = max(request_interval, state.get('request_interval', request_interval))
            if now < max(state.get('retry_at', 0), state.get('next_at', 0), state.get('lease_until', 0)):
                return {'control': state}
            name = None
            token = uuid.uuid4().hex
            if source == 'steam':
                for candidate, observed_attempt in candidates or []:
                    row = self.execute(db, 'SELECT attempted_at, lease_until FROM steam_jobs WHERE name = ?', (candidate,)).fetchone()
                    # A collector with an old cache must not repeat another collector's completed work.
                    if row and (row[0] > observed_attempt or row[1] > now
                                or (row[0] and now - row[0] < state['request_interval'])):
                        continue
                    name = candidate
                    seq = state['revision'] = state.get('revision', 0) + 1
                    self.execute(db, '''INSERT INTO steam_jobs (name, attempted_at, lease_token, lease_until, change_seq)
                        VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET
                        attempted_at=excluded.attempted_at, lease_token=excluded.lease_token,
                        lease_until=excluded.lease_until, change_seq=excluded.change_seq''',
                        (name, now, token, now + lease_seconds, seq))
                    state['scan_turn'] = state.get('scan_turn', 0) + 1
                    break
                if name is None:
                    return {'control': state}
            state.update(lease_token=token, lease_until=now + lease_seconds, worker=worker)
            self._write_control(db, source, state)
            return {'control': state, 'token': token, 'name': name, 'claimed_at': now}

    def complete(self, source, token, update, values=None, name=None, quote_data=None, checked=False, now=None):
        """A late response from an expired/replaced lease cannot overwrite a newer result."""
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            state = self._locked_control(db, source)
            if state.get('lease_token') != token or state.get('lease_until', 0) < now:
                return {'accepted': False, 'control': state}
            if name is not None:
                row = self.execute(db, 'SELECT lease_token, lease_until FROM steam_jobs WHERE name = ?', (name,)).fetchone()
                if not row or row[0] != token or row[1] < now:
                    return {'accepted': False, 'control': state}
                seq = state['revision'] = state.get('revision', 0) + 1
                if checked:
                    self.execute(db, '''UPDATE steam_jobs SET quote_payload = ?, checked_at = ?,
                        lease_token = NULL, lease_until = 0, change_seq = ? WHERE name = ?''',
                        (json.dumps(quote_data, ensure_ascii=False) if quote_data else None, now, seq, name))
                else:
                    self.execute(db, 'UPDATE steam_jobs SET lease_token = NULL, lease_until = 0, change_seq = ? WHERE name = ?', (seq, name))
            for key, value in (values or {}).items():
                self._put(db, key, value)
            state = update(state)
            state.update(lease_token=None, lease_until=0, last_cycle_at=now)
            self._write_control(db, source, state)
            return {'accepted': True, 'control': state}

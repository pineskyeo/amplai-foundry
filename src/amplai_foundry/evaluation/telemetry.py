"""Persistent bounded telemetry projection, separate from the authority event log.

Exporter delivery is at-least-once; ``event_id`` is the deduplication key. Queue
loss is explicitly counted, never applied to the authoritative Runtime Store.
Only whitelisted metadata leaves the process. This module is not an OTLP SDK.
An operator exporter may map these pinned records to OTLP logs/spans.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from amplai_foundry.runtime.contracts.identity import canonical, digest
from amplai_foundry.runtime.errors import RuntimeFault

MAPPING_VERSION = 'amplai.otel-metadata/1.0'


def project_event(scope, event: dict) -> dict:
    return {
        'mapping_version': MAPPING_VERSION,
        'event_id': event['event_id'], 'scope': scope.wire(),
        'source_sequence': event['seq'], 'aggregate_sequence': event['aggregate_seq'],
        'aggregate_type': event['aggregate_type'], 'aggregate_id': event['aggregate_id'],
        'event_type': event['event_type'], 'event_at': event['created_at'],
        'ingested_at': event['created_at'], 'timestamp_source': 'control_plane_ingestion',
        'payload_exported': False,
    }


class TelemetrySpool:
    def __init__(self, store, path: Path, *, max_events: int, max_bytes: int):
        if type(max_events) is not int or not 1 <= max_events <= 1_000_000:
            raise RuntimeFault('TELEMETRY_LIMIT', 'A bounded positive event capacity is required')
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 1_000_000_000:
            raise RuntimeFault('TELEMETRY_LIMIT', 'A bounded positive byte capacity is required')
        path = Path(path)
        if path.is_symlink() or path.parent.resolve() != path.parent.absolute():
            raise RuntimeFault('TELEMETRY_PATH', 'Spool path must not be a symlink')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store, self.max_events, self.max_bytes = store, max_events, max_bytes
        self._lock, self._drain_lock = threading.RLock(), threading.Lock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS cursor(scope TEXT PRIMARY KEY, seq INTEGER NOT NULL,
                dropped INTEGER NOT NULL, drop_bytes INTEGER NOT NULL, exported INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS queue(scope TEXT NOT NULL, seq INTEGER NOT NULL,
                data BLOB NOT NULL, size INTEGER NOT NULL, PRIMARY KEY(scope,seq));
        ''')

    def capture(self, scope, *, batch: int = 100) -> dict:
        if type(batch) is not int or not 1 <= batch <= 1000:
            raise RuntimeFault('TELEMETRY_LIMIT', 'Capture batch must be between 1 and 1000')
        key = digest(scope.wire())
        with self._lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('INSERT OR IGNORE INTO cursor VALUES(?,0,0,0,0)', (key,))
                current = self.db.execute('SELECT seq FROM cursor WHERE scope=?', (key,)).fetchone()[0]
                events = self.store.events(scope, after=current, limit=batch)
                count, size = self.db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM queue').fetchone()
                dropped = drop_bytes = 0
                for event in events:
                    raw = canonical(project_event(scope, event))
                    if count >= self.max_events or size + len(raw) > self.max_bytes:
                        dropped += 1
                        drop_bytes += len(raw)
                    else:
                        self.db.execute('INSERT INTO queue VALUES(?,?,?,?)', (key, event['seq'], raw, len(raw)))
                        count += 1
                        size += len(raw)
                if events:
                    self.db.execute('UPDATE cursor SET seq=?,dropped=dropped+?,drop_bytes=drop_bytes+? '
                                    'WHERE scope=?', (events[-1]['seq'], dropped, drop_bytes, key))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        return self.status(scope)

    def drain(self, scope, exporter, *, limit: int = 100) -> dict:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RuntimeFault('TELEMETRY_LIMIT', 'Drain batch must be between 1 and 1000')
        key = digest(scope.wire())
        # Serialize exporter/ack; no external IO while holding either DB transaction.
        with self._drain_lock:
            with self._lock:
                rows = self.db.execute('SELECT seq,data FROM queue WHERE scope=? ORDER BY seq LIMIT ?',
                                       (key, limit)).fetchall()
            if rows:
                self.store.assert_outside_tx()
                try:
                    exporter([json.loads(r['data']) for r in rows])
                except Exception as exc:
                    return {**self.status(scope), 'delivery': 'retry_pending', 'error_type': type(exc).__name__}
                with self._lock:
                    self.db.execute('BEGIN IMMEDIATE')
                    try:
                        self.db.executemany('DELETE FROM queue WHERE scope=? AND seq=?', [(key, r['seq']) for r in rows])
                        self.db.execute('UPDATE cursor SET exported=exported+? WHERE scope=?', (len(rows), key))
                        self.db.execute('COMMIT')
                    except BaseException:
                        self.db.execute('ROLLBACK')
                        raise
        return {**self.status(scope), 'delivery': 'acknowledged' if rows else 'empty'}

    def status(self, scope) -> dict:
        key = digest(scope.wire())
        with self._lock:
            row = self.db.execute('SELECT * FROM cursor WHERE scope=?', (key,)).fetchone()
            queued, size = self.db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM queue WHERE scope=?', (key,)).fetchone()
        return {'scope': scope.wire(), 'mapping_version': MAPPING_VERSION,
                'cursor': row['seq'] if row else 0, 'queued': queued, 'queued_bytes': size,
                'dropped': row['dropped'] if row else 0, 'dropped_bytes': row['drop_bytes'] if row else 0,
                'exported': row['exported'] if row else 0, 'authority_events_deleted': 0,
                'delivery_semantics': 'at_least_once'}

    def close(self):
        self.db.close()

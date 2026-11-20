import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.errors import RegisterError
from app.manifest import canonical_json

SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signatures (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sheet TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version = 1),
    PRIMARY KEY (run_id, sheet)
);
CREATE TABLE IF NOT EXISTS concurrences (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    edge TEXT NOT NULL,
    document TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version = 1),
    PRIMARY KEY (run_id, edge)
);
'''


class Store:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def connect(self, mode='DEFERRED'):
        connection = sqlite3.connect(self.database, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute(f'BEGIN {mode}')
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect('IMMEDIATE') as connection:
            for statement in SCHEMA.strip().removesuffix(';').split(';'):
                connection.execute(statement)

    @staticmethod
    def _read(connection, run_id, required=True):
        row = connection.execute('SELECT snapshot FROM runs WHERE run_id = ?', (run_id,)).fetchone()
        if not row:
            if required:
                raise RegisterError('RUN_NOT_FOUND', '未找到该批次', 404)
            return None
        signatures = connection.execute(
            'SELECT sheet, reviewer, signed_at, version FROM signatures WHERE run_id = ? ORDER BY sheet',
            (run_id,),
        )
        concurrences = connection.execute(
            'SELECT document FROM concurrences WHERE run_id = ? ORDER BY edge', (run_id,),
        ).fetchall()
        documents = [json.loads(item['document']) for item in concurrences]
        snapshot = {**json.loads(row['snapshot']), 'signatures': [dict(signature) for signature in signatures],
                    'concurrences': documents}
        edges_by_sheet = {}
        for document in documents:
            for sheet in document['disposition']['sheets']:
                edges_by_sheet.setdefault(sheet['code'], []).append(document['edge'])
        for review in snapshot['reviews']:
            review['concurrence_edges'] = edges_by_sheet.get(review['code'], [])
        return snapshot

    def get(self, run_id, required=True):
        with self.connect() as connection:
            return self._read(connection, run_id, required)

    def insert(self, snapshot):
        manifest = snapshot['manifest']
        run_id = manifest['run_id']
        serialized = canonical_json(snapshot)
        with self.connect('IMMEDIATE') as connection:
            insert = connection.execute('INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(run_id) DO NOTHING',
                                        (run_id, manifest['created_at'], serialized))
            return self._read(connection, run_id), insert.rowcount == 0

    def sign(self, run_id, sheet, reviewer, expected_version):
        with self.connect('IMMEDIATE') as connection:
            snapshot = self._read(connection, run_id)
            decision = next((row for row in snapshot['reviews'] if row['code'] == sheet), None)
            if decision is None:
                raise RegisterError('SHEET_NOT_FOUND', '该批次没有此图幅', 404)
            if decision['status'] != 'READY':
                raise RegisterError('SIGNATURE_BLOCKED', '图幅尚未满足签审条件', 409, decision['reason'])
            existing = next((row for row in snapshot['signatures'] if row['sheet'] == sheet), None)
            if existing and existing['reviewer'] == reviewer and expected_version in (0, existing['version']):
                return existing
            if existing or expected_version != 0:
                raise RegisterError('SIGNATURE_VERSION_CONFLICT', '签审版本已变化，请刷新', 409,
                                    existing['version'] if existing else 0)
            signed_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            connection.execute('INSERT INTO signatures VALUES (?, ?, ?, ?, 1)',
                               (run_id, sheet, reviewer, signed_at))
            return {'sheet': sheet, 'reviewer': reviewer, 'signed_at': signed_at, 'version': 1}

    def concur(self, run_id, edge, document):
        # 整笔事务：任一步失败均回滚，旧版会签原样保留
        with self.connect('IMMEDIATE') as connection:
            self._read(connection, run_id)
            row = connection.execute(
                'SELECT reviewer, signed_at, version FROM concurrences WHERE run_id = ? AND edge = ?',
                (run_id, edge)).fetchone()
            reviewer = document['reviewer']
            if row is not None:
                if row['reviewer'] == reviewer and document['expected_version'] in (0, row['version']):
                    return self._read(connection, run_id)['concurrences']
                if row['reviewer'] != reviewer:
                    raise RegisterError('CONCURRENCE_EXISTS',
                                        '同一批次重跑不能新增正式会签；版本冲突请刷新后重试', 409,
                                        {'edge': edge, 'version': row['version']})
                raise RegisterError('CONCURRENCE_VERSION_CONFLICT', '会签版本已变化，请刷新后重试', 409,
                                    row['version'])
            if document['expected_version'] != 0:
                raise RegisterError('CONCURRENCE_VERSION_CONFLICT', '会签版本已变化，请刷新后重试', 409, 0)
            signed_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            record = {**{key: value for key, value in document.items() if key != 'expected_version'},
                      'signed_at': signed_at, 'version': 1}
            connection.execute('INSERT INTO concurrences VALUES (?, ?, ?, ?, ?, 1)',
                               (run_id, edge, canonical_json(record), reviewer, signed_at))
            return self._read(connection, run_id)['concurrences']

"""Durable command receipts. An uncertain mutation is never replayed."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4


class ReceiptConflict(ValueError):
    """A request ID was reused for a different operation."""


class ReceiptStore:
    """Small, separate SQLite journal for transport-level request deduplication.

    The collection transaction and this journal cannot commit atomically with
    filesystem changes. After a crash, pending receipts therefore become
    uncertain; callers must inspect the collection instead of replaying them.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS remote_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO remote_metadata (key,value) VALUES ('server_id',?)", (str(uuid4()),))
            self.server_id = db.execute("SELECT value FROM remote_metadata WHERE key='server_id'").fetchone()[0]
            db.execute("""CREATE TABLE IF NOT EXISTS remote_receipts (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL, state TEXT NOT NULL,
                result_json TEXT, error_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""UPDATE remote_receipts SET state='uncertain',
                error_json=?, updated_at=CURRENT_TIMESTAMP WHERE state='running'""",
                       (json.dumps({"detail": "The workstation restarted before recording the outcome. Inspect the session before issuing another command.",
                                    "status_code": 409}),))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict:
        return {"request_id": row["request_id"], "state": row["state"],
                "result": json.loads(row["result_json"]) if row["result_json"] is not None else None,
                "error": json.loads(row["error_json"]) if row["error_json"] is not None else None}

    def get(self, request_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM remote_receipts WHERE request_id=?", (request_id,)).fetchone()
        return self._receipt(row) if row is not None else None

    def payload(self, request_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT payload_json FROM remote_receipts WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def uncertain_for(self, session_id: str | None, root: str | None = None) -> bool:
        with self._connect() as db:
            rows = db.execute("SELECT payload_json FROM remote_receipts WHERE state='uncertain'").fetchall()
        return any(value.get("mutation", True) and (
                   (session_id and value.get("session_id") == session_id)
                   or (root and value.get("root") == root))
                   for value in (json.loads(row[0]) for row in rows))

    def claim(self, request_id: str, payload: dict) -> tuple[dict, bool]:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM remote_receipts WHERE request_id=?", (request_id,)).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise ReceiptConflict("Request ID already belongs to a different command")
                return self._receipt(row), False
            db.execute("""INSERT INTO remote_receipts
                (request_id,fingerprint,payload_json,state) VALUES (?,?,?,'running')""",
                       (request_id, fingerprint, encoded))
        return {"request_id": request_id, "state": "running", "result": None, "error": None}, True

    def finish(self, request_id: str, *, result=None, error: dict | None = None,
               uncertain: bool = False) -> dict:
        state = "uncertain" if uncertain else "failed" if error is not None else "completed"
        with self._connect() as db:
            db.execute("""UPDATE remote_receipts SET state=?, result_json=?,
                error_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE request_id=? AND state='running'""",
                       (state, json.dumps(result, allow_nan=False),
                        json.dumps(error) if error is not None else None, request_id))
        receipt = self.get(request_id)
        if receipt is None:
            raise RuntimeError("Command receipt disappeared")
        return receipt

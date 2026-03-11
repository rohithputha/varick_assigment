"""
SQLiteDB — thin sqlite3 wrapper with WAL mode and table creation.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


_DEFAULT_DB_PATH = Path(__file__).parent.parent / "pipeline.db"


class SQLiteDB:
    """
    Thin wrapper around sqlite3. Holds the connection and creates tables.
    Use as a context manager for transactions (not required — auto-commit via conn.commit()).

    For testing, pass db_path=":memory:".
    """

    def __init__(self, db_path: str | Path = _DEFAULT_DB_PATH) -> None:
        self.db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        """
        Open connection with WAL mode for concurrent reads.
        Sets row_factory = sqlite3.Row for dict-like column access.
        Idempotent — returns the same connection if already open.
        """
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def create_tables(self) -> None:
        """
        Create all pipeline tables. Idempotent — safe to call on every startup.
        """
        conn = self.connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                run_id          TEXT PRIMARY KEY,
                invoice_id      TEXT,
                status          TEXT NOT NULL,
                current_stage   TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                metadata        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stage_results (
                result_id       TEXT PRIMARY KEY,
                run_id          TEXT NOT NULL REFERENCES pipeline_runs(run_id),
                stage           TEXT NOT NULL,
                status          TEXT NOT NULL,
                input_payload   TEXT NOT NULL,
                output_payload  TEXT,
                halted          INTEGER NOT NULL DEFAULT 0,
                halt_reason     TEXT,
                started_at      TEXT NOT NULL,
                completed_at    TEXT,
                UNIQUE(run_id, stage)
            );

            CREATE TABLE IF NOT EXISTS halt_records (
                halt_id             TEXT PRIMARY KEY,
                run_id              TEXT NOT NULL REFERENCES pipeline_runs(run_id),
                stage               TEXT NOT NULL,
                reason              TEXT NOT NULL,
                ingestion_state_id  TEXT,
                correction_input    TEXT,
                resolved            INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT NOT NULL,
                resolved_at         TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_stage_results_run_id ON stage_results(run_id);
            CREATE INDEX IF NOT EXISTS idx_halt_records_run_id  ON halt_records(run_id);
            CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status);
        """)
        conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_ts TEXT NOT NULL,
  run_date TEXT NOT NULL,
  files_seen INTEGER NOT NULL,
  files_new INTEGER NOT NULL,
  matches_found INTEGER NOT NULL,
  email_sent INTEGER NOT NULL,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS processed_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT NOT NULL,
  file_url TEXT NOT NULL UNIQUE,
  file_name TEXT NOT NULL,
  processed_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT NOT NULL,
  source_file_url TEXT NOT NULL,
  publication_title TEXT,
  orgao TEXT,
  keyword_hit TEXT NOT NULL,
  text_full TEXT NOT NULL,
  dou_link TEXT,
  created_ts TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class MatchRow:
    run_date: str
    source_file_url: str
    publication_title: Optional[str]
    orgao: Optional[str]
    keyword_hit: str
    text_full: str
    dou_link: Optional[str]


class Storage:
    def __init__(self, sqlite_path: str):
        # Normaliza para caminho absoluto no runner e garante diretório
        self.sqlite_path = os.path.abspath(sqlite_path)
        db_dir = os.path.dirname(self.sqlite_path)
        os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # Defensivo: garante diretório de novo antes de conectar
        db_dir = os.path.dirname(self.sqlite_path)
        os.makedirs(db_dir, exist_ok=True)
        con = sqlite3.connect(self.sqlite_path)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        con = self._connect()
        try:
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()

    def was_file_processed(self, file_url: str) -> bool:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT 1 FROM processed_files WHERE file_url = ?",
                (file_url,),
            ).fetchone()
            return row is not None
        finally:
            con.close()

    def mark_file_processed(self, run_date: str, file_url: str, file_name: str) -> None:
        con = self._connect()
        try:
            con.execute(
                """
                INSERT OR IGNORE INTO processed_files(run_date, file_url, file_name, processed_ts)
                VALUES (?, ?, ?, ?)
                """,
                (run_date, file_url, file_name, datetime.utcnow().isoformat()),
            )
            con.commit()
        finally:
            con.close()

    def insert_matches(self, rows: Iterable[MatchRow]) -> int:
        rows_list = list(rows)
        if not rows_list:
            return 0

        con = self._connect()
        try:
            con.executemany(
                """
                INSERT INTO matches(
                    run_date, source_file_url, publication_title, orgao,
                    keyword_hit, text_full, dou_link, created_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r.run_date,
                        r.source_file_url,
                        r.publication_title,
                        r.orgao,
                        r.keyword_hit,
                        r.text_full,
                        r.dou_link,
                        datetime.utcnow().isoformat(),
                    )
                    for r in rows_list
                ],
            )
            con.commit()
            return len(rows_list)
        finally:
            con.close()

    def log_run(
        self,
        run_date: str,
        files_seen: int,
        files_new: int,
        matches_found: int,
        email_sent: bool,
        notes: str = "",
    ) -> None:
        con = self._connect()
        try:
            con.execute(
                """
                INSERT INTO runs(run_ts, run_date, files_seen, files_new, matches_found, email_sent, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.utcnow().isoformat(),
                    run_date,
                    files_seen,
                    files_new,
                    matches_found,
                    1 if email_sent else 0,
                    notes,
                ),
            )
            con.commit()
        finally:
            con.close()

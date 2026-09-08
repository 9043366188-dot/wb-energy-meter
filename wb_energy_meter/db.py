"""SQLite + миграции."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)


DEFAULT_DB_PATH = "/mnt/data/var/lib/wb-energy-meter/state.db"
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_([a-zA-Z0-9_]+)\.sql$")


def _py_casefold(s):
    """SQL-функция py_casefold(x) — правильная казефолд-нормализация строк,
    в отличие от SQLite COLLATE NOCASE (только ASCII A-Z) корректно
    сворачивает регистр и для кириллицы. Используется в миграциях для
    построения уникального индекса по нормализованному имени зоны."""
    if s is None:
        return None
    return str(s).strip().casefold()


class Database:
    def __init__(self, path=DEFAULT_DB_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._conn = None

    def open(self):
        with self._lock:
            if self._conn is not None: return
            db_dir = os.path.dirname(self._path)
            if db_dir: os.makedirs(db_dir, exist_ok=True)
            log.info("Открываю БД: %s", self._path)
            self._conn = sqlite3.connect(
                self._path, check_same_thread=False,
                isolation_level=None, timeout=10.0,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA temp_store = MEMORY")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            # py_casefold() — доступна миграциям и запросам для нормализации
            # имён зон с учётом кириллицы (SQLite COLLATE NOCASE её не берёт).
            self._conn.create_function("py_casefold", 1, _py_casefold)
            self._apply_migrations()
            try:
                size = os.path.getsize(self._path)
                log.info("БД готова, размер: %.1f КБ, версия схемы: %d",
                         size/1024, self.current_schema_version())
            except OSError: pass

    def close(self):
        with self._lock:
            if self._conn is not None:
                try: self._conn.close()
                except sqlite3.Error: pass
                self._conn = None

    @property
    def path(self): return self._path

    def conn(self):
        if self._conn is None:
            raise RuntimeError("Database is not opened")
        return self._conn

    @contextmanager
    def transaction(self):
        with self._lock:
            c = self.conn()
            c.execute("BEGIN")
            try: yield c
            except Exception:
                c.execute("ROLLBACK"); raise
            else:
                c.execute("COMMIT")

    @contextmanager
    def read(self):
        with self._lock:
            yield self.conn()

    def current_schema_version(self):
        with self._lock:
            cur = self.conn().execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'")
            if cur.fetchone() is None: return 0
            row = self.conn().execute(
                "SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            return int(row["v"] or 0)

    def _apply_migrations(self):
        if not _MIGRATIONS_DIR.is_dir():
            log.warning("Папка миграций не найдена: %s", _MIGRATIONS_DIR); return
        files = []
        for p in sorted(_MIGRATIONS_DIR.iterdir()):
            if not p.is_file(): continue
            m = _MIGRATION_RE.match(p.name)
            if not m: continue
            files.append((int(m.group(1)), m.group(2), p))
        files.sort(key=lambda x: x[0])
        if not files: return
        current = self.current_schema_version()
        for version, name, path in files:
            if version <= current: continue
            log.info("Применяю миграцию %03d_%s ...", version, name)
            sql = path.read_text(encoding="utf-8")
            try: self.conn().executescript(sql)
            except sqlite3.Error as e:
                log.error("Миграция %03d_%s упала: %s", version, name, e)
                raise
            log.info("Миграция %03d_%s применена", version, name)

    def vacuum(self):
        with self._lock:
            log.info("VACUUM ...")
            t0 = time.time()
            self.conn().execute("VACUUM")
            log.info("VACUUM завершён за %.2f с", time.time() - t0)

    def stats(self):
        with self._lock:
            c = self.conn()
            try: size = os.path.getsize(self._path)
            except OSError: size = 0
            tables = ["meters", "meter_groups", "period_aggregates",
                      "alert_events", "snoozes", "kv"]
            counts = {}
            for t in tables:
                try:
                    row = c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
                    counts[t] = int(row["n"])
                except sqlite3.Error: counts[t] = None
            return {
                "path": self._path,
                "size_bytes": size,
                "schema_version": self.current_schema_version(),
                "table_counts": counts,
            }

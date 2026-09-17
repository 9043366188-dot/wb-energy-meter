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


def _split_sql_statements(script: str):
    """Разбить SQL-скрипт на отдельные операторы для пошагового
    выполнения внутри явной транзакции (см. `_apply_migration_atomic`).
    Корректно пропускает ';' внутри '...'/"..." строк (с удвоенными
    кавычками как экранированием, по правилам SQLite) и внутри
    однострочных '--' комментариев. Не рассчитан на блочные /* */
    комментарии и на тела триггеров с ';' внутри BEGIN...END — в
    миграциях этого проекта их нет."""
    statements = []
    in_string = False
    quote_char = ""
    i = 0
    n = len(script)
    stmt_start = 0
    while i < n:
        ch = script[i]
        if in_string:
            if ch == quote_char:
                if i + 1 < n and script[i + 1] == quote_char:
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = True
            quote_char = ch
            i += 1
            continue
        if ch == '-' and i + 1 < n and script[i + 1] == '-':
            j = script.find('\n', i)
            i = n if j == -1 else j + 1
            continue
        if ch == ';':
            stmt = script[stmt_start:i].strip()
            if stmt:
                statements.append(stmt)
            stmt_start = i + 1
        i += 1
    tail = script[stmt_start:].strip()
    if tail:
        statements.append(tail)
    return statements


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

    def backup_to(self, dest_path: str) -> None:
        """Безопасная копия БД через SQLite Online Backup API.

        Используется перед разовыми необратимыми по сути операциями на
        реальных данных (например migrate_meters_and_groups,
        legacy_migration.py) — НЕ через shutil.copy2/copyfile: обычное
        копирование файла поверх WAL-режима (см. `open()`) может
        захватить несогласованный набор страниц (часть изменений ещё в
        -wal, а не в основном файле) и дать битую копию. Backup API сам
        обеспечивает консистентный снимок постранично, не блокируя
        читателей/писателей на всё время копирования.
        """
        with self._lock:
            dest_dir = os.path.dirname(dest_path)
            if dest_dir: os.makedirs(dest_dir, exist_ok=True)
            dest_conn = sqlite3.connect(dest_path)
            try:
                self.conn().backup(dest_conn)
            finally:
                dest_conn.close()

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
            if version >= 5:
                # Миграции 001-004 уже применены на объекте старым путём
                # (executescript без явной транзакции) — их код не трогаем.
                # С версии 5 — атомарный протокол, см. docs/migration-plan-v2.md §4.
                self._apply_migration_atomic(version, name, sql)
            else:
                try: self.conn().executescript(sql)
                except sqlite3.Error as e:
                    log.error("Миграция %03d_%s упала: %s", version, name, e)
                    raise
            log.info("Миграция %03d_%s применена", version, name)

    def _apply_migration_atomic(self, version, name, sql):
        """Атомарный протокол для миграций версии >= 5 (docs/migration-plan-v2.md §4).

        `Connection.executescript()` сама коммитит текущую транзакцию перед
        запуском скрипта (задокументированное поведение sqlite3 в Python —
        она ориентирована на автономные скрипты, а не на встраивание в
        внешнюю транзакцию), поэтому явный BEGIN перед executescript()
        молча проглатывается и НЕ защищает от частичного применения. Чтобы
        держать транзакцию самим, скрипт разбирается на отдельные операторы
        и каждый выполняется через execute() внутри одного BEGIN/COMMIT.
        При любой ошибке — ROLLBACK, БД остаётся в состоянии ДО миграции,
        следующий запуск может безопасно повторить попытку.
        """
        conn = self.conn()
        statements = _split_sql_statements(sql)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            conn.execute("ROLLBACK")
            log.error("Миграция %03d_%s упала и полностью откачена: %s",
                      version, name, e)
            raise
        log.info("Миграция %03d_%s применена атомарно (%d операторов)",
                  version, name, len(statements))

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

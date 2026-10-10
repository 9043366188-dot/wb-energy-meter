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

from . import change_journal

log = logging.getLogger(__name__)


DEFAULT_DB_PATH = "/mnt/data/var/lib/wb-energy-meter/state.db"

# Размер пула соединений ТОЛЬКО ДЛЯ ЧТЕНИЯ (партия 11, этап 11.3). Сервер
# Werkzeug в threaded-режиме создаёт поток на запрос, поэтому
# threading.local() давал бы по соединению на каждый такой поток и они
# утекали бы; ограниченный пул с ленивым созданием держит число открытых
# дескрипторов постоянным. 4 — потолок параллельных тяжёлых расчётов на
# контроллере (одно ядро, ~1 ГБ ОЗУ), остальные читатели ждут свободное
# соединение, а не открывают новые.
READ_POOL_SIZE = 4
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


def _is_in_memory_path(path) -> bool:
    """":memory:" (и пустой путь — приватная временная БД) живут только в
    одном соединении: второе соединение увидело бы свою пустую базу, поэтому
    для них пул читателей не создаётся (см. Database.read)."""
    p = str(path or "")
    return p == "" or p == ":memory:" or p.startswith("file::memory:") \
        or "mode=memory" in p


class Database:
    """Слой БД: ОДНО соединение-писатель под RLock + ограниченный пул
    соединений только для чтения.

    Зачем (docs/load-test-2026-09.md, «Находка 3»): раньше read() и
    transaction() держали один и тот же RLock на всё время блока, и тяжёлый
    расчёт внутри `with db.read()` (reports/query) не давал писать никому,
    включая агрегатор — запись ждала до ~450 мс. Теперь:

    - transaction() — как раньше: писатель, RLock, реентерабельность по
      `_txn_depth`, BEGIN/COMMIT/ROLLBACK только у внешнего вызова;
    - read() — отдельное соединение из пула (`PRAGMA query_only=ON`) и ОДНА
      читающая транзакция на весь блок, поэтому все SELECT внутри видят
      одно состояние БД (снимок WAL, A43), а запись параллельно не ждёт;
    - вложенный read() в том же потоке переиспользует то же соединение и
      тот же снимок;
    - read() внутри открытой transaction() этого же потока отдаёт писателя,
      иначе код не увидел бы свои же незакоммиченные строки;
    - ":memory:" — откат на одно соединение и общий замок, как раньше.

    Следствия для вызывающего кода: через соединение из read() писать
    нельзя (sqlite3.OperationalError «readonly database») — запись только
    через transaction(); запись, выполненная внутри read() через
    transaction(), снимку этого read() не видна (она в другом соединении),
    а видна следующему read()."""

    def __init__(self, path=DEFAULT_DB_PATH, read_pool_size=READ_POOL_SIZE):
        self._path = path
        self._lock = threading.RLock()
        self._conn = None
        self._txn_depth = 0
        self._txn_owner = None   # ident потока, держащего внешнюю transaction()
        # перехват изменений для change_log (change_journal.py); ставится в open()
        self._capture = None
        # --- пул читателей (всё ниже защищено _pool_cond, не _lock) ---
        self._read_pool_size = max(0, int(read_pool_size))
        self._pool_cond = threading.Condition(threading.Lock())
        self._pool_idle = []     # свободные соединения (LIFO)
        self._pool_created = 0   # сколько соединений живо в текущем поколении
        self._pool_gen = 0       # растёт при close(): старые закрываются при возврате
        self._pool_open = False
        self._tls = threading.local()   # .reader = (conn, gen) активного read() потока

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
            with self._pool_cond:
                self._pool_open = True
            self._apply_migrations()
            # Журнал изменений (партия 12): временные триггеры на писателе,
            # ПОСЛЕ миграций (таблицы уже есть). Без журнала БД работает, но
            # история не пишется — поэтому сбой установки громко логируется.
            try:
                self._capture = change_journal.install(self._conn)
            except sqlite3.Error:
                log.exception("Не удалось включить журнал изменений change_log")
                self._capture = None
            try:
                size = os.path.getsize(self._path)
                log.info("БД готова, размер: %.1f КБ, версия схемы: %d",
                         size/1024, self.current_schema_version())
            except OSError: pass

    def close(self):
        with self._lock:
            self._capture = None
            if self._conn is not None:
                try: self._conn.close()
                except sqlite3.Error: pass
                self._conn = None
            self._close_read_pool()

    # ------------------------------------------------------------------
    # Пул соединений для чтения (партия 11, этап 11.3)
    # ------------------------------------------------------------------

    def _read_pool_enabled(self) -> bool:
        return self._read_pool_size > 0 and not _is_in_memory_path(self._path)

    def _connect_reader(self):
        """Читающее соединение: те же прагмы, что у писателя в open()
        (кроме journal_mode — режим WAL записан в самом файле БД и
        наследуется), плюс py_casefold() (индексы и запросы миграций её
        используют) и query_only=ON — запись через это соединение невозможна."""
        c = sqlite3.connect(
            self._path, check_same_thread=False,
            isolation_level=None, timeout=10.0,
        )
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA temp_store = MEMORY")
            c.execute("PRAGMA busy_timeout = 5000")
            c.create_function("py_casefold", 1, _py_casefold)
            c.execute("PRAGMA query_only = ON")
        except Exception:
            c.close()
            raise
        return c

    def _acquire_reader(self):
        """Свободное читающее соединение: из пула, иначе новое (пока не
        достигнут READ_POOL_SIZE), иначе ждём возврата. Возвращает
        (соединение, поколение пула)."""
        with self._pool_cond:
            while True:
                if not self._pool_open:
                    raise RuntimeError("Database is not opened")
                if self._pool_idle:
                    return self._pool_idle.pop(), self._pool_gen
                if self._pool_created < self._read_pool_size:
                    conn = self._connect_reader()
                    self._pool_created += 1
                    return conn, self._pool_gen
                self._pool_cond.wait()

    def _release_reader(self, conn, gen):
        with self._pool_cond:
            if self._pool_open and gen == self._pool_gen:
                self._pool_idle.append(conn)
                self._pool_cond.notify()
                return
        # пул закрыт (или переоткрыт) за время чтения — соединение чужого
        # поколения не возвращаем, закрываем
        try: conn.close()
        except sqlite3.Error: pass

    def _close_read_pool(self):
        """Закрыть все свободные читающие соединения; занятые (читатель ещё
        внутри read() в другом потоке) закроются при возврате — закрывать
        соединение под ногами у работающего потока нельзя."""
        with self._pool_cond:
            idle, self._pool_idle = self._pool_idle, []
            self._pool_open = False
            self._pool_created = 0
            self._pool_gen += 1
            self._pool_cond.notify_all()
        for conn in idle:
            try: conn.close()
            except sqlite3.Error: pass

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
        """Атомарная транзакция записи. Реентрантна в пределах одного потока:
        вложенный `with db.transaction()` (например репозиторий, вызванный
        изнутри сервиса ревизий — см. revision_service.py) присоединяется к
        уже открытой транзакции вместо попытки открыть вторую (SQLite не
        допускает вложенный BEGIN на одном соединении) — BEGIN/COMMIT/ROLLBACK
        выполняет только самый внешний вызов. Это то, что позволяет
        revision_service.with_revision_check() держать проверку
        expected_revision, доменную запись и создание новой
        configuration_revisions СТРОГО в одной транзакции (ТЗ §6.1:
        "закрытие старой версии и создание новой выполняются в одной
        транзакции под блокировкой записи"), не трогая сами репозитории."""
        with self._lock:
            c = self.conn()
            depth = self._txn_depth
            if depth == 0:
                c.execute("BEGIN")
                self._txn_owner = threading.get_ident()
            self._txn_depth = depth + 1
            try:
                yield c
            except Exception:
                self._txn_depth = depth
                if depth == 0:
                    self._txn_owner = None
                    c.execute("ROLLBACK")
                raise
            else:
                self._txn_depth = depth
                if depth == 0:
                    self._txn_owner = None
                    if self._capture is not None:
                        # журнал изменений — в ТОЙ ЖЕ транзакции, до COMMIT;
                        # сбой журнала откатывает и саму правку
                        try:
                            self._capture.drain(c)
                        except BaseException:
                            c.execute("ROLLBACK")
                            raise
                    c.execute("COMMIT")

    @contextmanager
    def read(self):
        """Блок чтения: все SELECT внутри видят ОДНО состояние БД (A43).

        Не держит блокировку писателя: пока идёт тяжёлый расчёт, transaction()
        других потоков (агрегатор, правки из UI) выполняется без ожидания.
        Соединение — только для чтения; для записи используйте transaction()."""
        # 1. Внутри открытой transaction() ЭТОГО потока — писатель: иначе код
        #    не увидел бы собственные незакоммиченные строки.
        if self._txn_owner == threading.get_ident():
            yield self.conn()
            return
        # 2. Вложенный read() в том же потоке — то же соединение и тот же снимок.
        active = getattr(self._tls, "reader", None)
        if active is not None:
            yield active[0]
            return
        # 3. ":memory:" — одно соединение и общий замок, как раньше.
        if not self._read_pool_enabled():
            with self._lock:
                yield self.conn()
            return
        # 4. Обычный путь: соединение из пула + одна читающая транзакция.
        conn, gen = self._acquire_reader()
        try:
            conn.execute("BEGIN")
        except BaseException:
            self._release_reader(conn, gen)
            raise
        self._tls.reader = (conn, gen)
        try:
            yield conn
        except BaseException:
            self._finish_read(conn, gen, "ROLLBACK")
            raise
        else:
            self._finish_read(conn, gen, "COMMIT")

    def _finish_read(self, conn, gen, statement):
        self._tls.reader = None
        try:
            conn.execute(statement)
        except sqlite3.Error:
            # соединение возвращается в пул только «чистым»
            try: conn.execute("ROLLBACK")
            except sqlite3.Error: pass
        self._release_reader(conn, gen)

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

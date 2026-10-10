#!/usr/bin/env python3
"""Партия 10, этап E: поднимает сервис (Flask-приложение wb_energy_meter)
на УЖЕ СГЕНЕРИРОВАННОЙ нагрузочной БД (см. seed_load_db.py) в отдельном
процессе — запускается как subprocess из measure.py, никогда напрямую
руками на боевых данных.

Без MQTT/фонового обновления зон — нагрузочный сценарий (§5 задания)
трогает только чтение (`snapshot`, `overview/summary`, `reports/query`,
баланс узла) и `/api/status`, для которых MQTT не нужен.

Сверх обычного `create_app()` добавляет ТРИ служебных маршрута только
для измерений этого этапа (нигде в wb_energy_meter их нет и не будет —
это оснастка теста, а не часть продукта):

  GET  /__loadtest/trace/start  -- обнулить счётчик SQL-запросов и
                                    включить sqlite3.set_trace_callback
                                    на соединении БД (для подсчёта N+1,
                                    §5: "считать через sqlite3.set_trace_callback");
  GET  /__loadtest/trace/stop   -- выключить трассировку, вернуть счётчик;
  POST /__loadtest/write_probe  -- одна дешёвая запись через
                                    db.transaction() (замер длительности
                                    удержания блокировки записи, §5:
                                    "расчёт не должен держать запись").

Печатает в stdout строку `DB_OPEN_TIME_S=<секунды>` сразу после
`db.open()` -- время открытия/применения миграций именно на ЭТОЙ (уже
большой) базе, и `READY port=<port>` перед стартом сервера, чтобы
measure.py мог определить готовность без опроса /health вслепую.
"""

from __future__ import annotations

import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.aggregates_repo import AggregateRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry


def main():
    if len(sys.argv) < 3:
        print("usage: server_entry.py <db_path> <port>", file=sys.stderr)
        return 2
    db_path = sys.argv[1]
    port = int(sys.argv[2])

    db = Database(path=db_path)
    t0 = time.time()
    db.open()
    dt = time.time() - t0
    print(f"DB_OPEN_TIME_S={dt:.4f}", flush=True)

    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)
    registry = MeterRegistry()
    state = _AppState(
        registry=registry, meters_repo=meters_repo, groups_repo=groups_repo,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None,
        consumption_service=None, started_at=time.time(),
        aggregates_repo=AggregateRepo(db), db=db,
    )
    app = create_app(state)

    query_counter = {"n": 0, "tracing": False}

    def _tracer(sql):
        query_counter["n"] += 1

    # Партия 11, этап 11.3: чтение теперь идёт через пул отдельных соединений
    # (db.read()), а не через соединение-писатель, поэтому трассировку надо
    # вешать и на них — иначе счётчик запросов видел бы только записи. Колбэк
    # вешается на каждое новое читающее соединение один раз и сам проверяет
    # флаг; служебные BEGIN/COMMIT/ROLLBACK читающей транзакции не считаем,
    # чтобы число запросов оставалось сопоставимым с замерами до 0.21.0
    # (там read() транзакций не открывал).
    def _reader_tracer(sql):
        if not query_counter["tracing"]:
            return
        if sql.lstrip()[:8].upper().startswith(("BEGIN", "COMMIT", "ROLLBACK")):
            return
        query_counter["n"] += 1

    _orig_connect_reader = db._connect_reader

    def _traced_connect_reader():
        c = _orig_connect_reader()
        c.set_trace_callback(_reader_tracer)
        return c

    db._connect_reader = _traced_connect_reader

    @app.route("/__loadtest/trace/start")
    def _lt_trace_start():
        query_counter["n"] = 0
        query_counter["tracing"] = True
        db.conn().set_trace_callback(_tracer)
        return {"ok": True}

    @app.route("/__loadtest/trace/stop")
    def _lt_trace_stop():
        db.conn().set_trace_callback(None)
        query_counter["tracing"] = False
        return {"ok": True, "queries": query_counter["n"]}

    @app.route("/__loadtest/write_probe", methods=["POST"])
    def _lt_write_probe():
        t_probe0 = time.time()
        with db.transaction() as c:
            c.execute(
                "UPDATE kv SET updated_at = updated_at WHERE key = '__loadtest_probe__'")
            if c.execute(
                "SELECT 1 FROM kv WHERE key = '__loadtest_probe__'"
            ).fetchone() is None:
                c.execute(
                    "INSERT INTO kv (key, value, updated_at) VALUES "
                    "('__loadtest_probe__', '1', ?)", (int(time.time()),))
        return {"ok": True, "elapsed_s": time.time() - t_probe0}

    print(f"READY port={port}", flush=True)
    from werkzeug.serving import run_simple
    run_simple("127.0.0.1", port, app, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

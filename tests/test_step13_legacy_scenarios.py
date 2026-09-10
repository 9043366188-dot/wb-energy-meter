"""Тесты Шага 13 — воспроизводимые сценарии этапа A ТЗ
(docs/TZ-metering-architecture-dashboard.md, §12: «Условие перехода: Есть
воспроизводимые сценарии двойного счёта, пропусков и миграции»).

Это ХАРАКТЕРИЗУЮЩИЕ тесты: они фиксируют, что именно СЕЙЧАС (v0.11.1)
даёт неверный или вводящий в заблуждение результат, привязаны к
конкретным сценариям §13 ТЗ (A03/A04/A08/A12/A13) и к конкретным местам
кода. Они не проверяют новую архитектуру — новой архитектуры ещё нет,
она появится в этапах B/C/D. Они количественно документируют дефект
старой, чтобы у перехода к следующему этапу был проверяемый критерий
«исправлено».

Самостоятельный скрипт (не pytest):
    python tests/test_step13_legacy_scenarios.py
"""

from __future__ import annotations

import json
import os
import sys
import sqlite3
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.consumption import (
    calculate_from_points, ConsumptionResult, ConsumptionService,
)
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate, align_hour_down
from wb_energy_meter.periods import Period
from wb_energy_meter.db import Database

from fixtures.legacy_db_0_11_1 import (
    build_legacy_db, reset_history_points, SCENARIO_ENERGY_KWH,
)


class FixedConsumptionService:
    """Заглушка ConsumptionService.calculate — фиксированные кВт·ч по
    device_id, без MQTT/RPC. Воспроизводит §5.2 ТЗ: ГРЩ-1=100,
    Цех=60 (включая Станок=20), Серверная=30, Станок=20."""

    def __init__(self, energy_by_device_id):
        self._energy = energy_by_device_id

    def calculate(self, device_id, period, channel=None):
        kwh = self._energy.get(device_id)
        return ConsumptionResult(
            device_id=device_id, period=period, consumption_kwh=kwh,
            ap_energy_start=0.0, ap_energy_end=kwh,
            ts_start_actual=period.ts_from, ts_end_actual=period.ts_to,
            samples_in_period=2, quality="ok" if kwh is not None else "no_data",
        )


def test_double_counting_a04_reproducible():
    """A03/A04: ввод + его же физический потомок («Станок» внутри
    «Цеха») должны были бы дать 409 с объяснением пересечения пути
    (ТЗ §5.1). Сегодня `api.py::api_reports_balance` (api.py:754-813)
    ничего не знает об электрической вложенности — группы 0.11.1 плоские
    (`meter_groups.parent_id` есть в схеме, но `GroupRepo.create`
    (repo.py:156-178) создаёт плоскую группу) — и просто суммирует все
    role=consumer по отдельности."""
    fx = build_legacy_db()
    try:
        energy = {
            fx.meters["input"].device_id: SCENARIO_ENERGY_KWH["input"],
            fx.meters["tsex"].device_id: SCENARIO_ENERGY_KWH["tsex"],
            fx.meters["server"].device_id: SCENARIO_ENERGY_KWH["server"],
            fx.meters["stanok"].device_id: SCENARIO_ENERGY_KWH["stanok"],
        }
        state = _AppState(
            registry=MeterRegistry(), meters_repo=fx.meters_repo,
            groups_repo=fx.groups_repo, alert_repo=None,
            is_mqtt_connected=lambda: True,
            mqtt_message_count=lambda: 0, mqtt_error_count=lambda: 0,
            wb_db_client=None,
            consumption_service=FixedConsumptionService(energy),
            started_at=0.0,
        )
        app = create_app(state)
        client = app.test_client()

        resp = client.get("/api/reports/balance?period=today")
        assert resp.status_code == 200, resp.data
        body = json.loads(resp.data)

        input_total = body["input"]["total_kwh"]
        consumer_total = body["consumer"]["total_kwh"]
        imbalance = body["imbalance_kwh"]

        correct_consumer_total = (SCENARIO_ENERGY_KWH["tsex"]
                                   + SCENARIO_ENERGY_KWH["server"])  # 90, без станка

        assert input_total == SCENARIO_ENERGY_KWH["input"]  # 100.0
        # ДЕФЕКТ: Станок (уже внутри Цеха) снова прибавлен к consumer_total.
        assert consumer_total == correct_consumer_total + SCENARIO_ENERGY_KWH["stanok"]  # 110.0
        assert consumer_total != correct_consumer_total
        assert imbalance == round(SCENARIO_ENERGY_KWH["input"] - consumer_total, 6)
        assert imbalance < 0, (
            "текущий расчёт из-за двойного счёта Станка внутри Цеха "
            "уходит в отрицательный 'небаланс', как будто на объекте "
            "генерация, которой нет")
        print(f"[REPRODUCED] A03/A04: consumer_total={consumer_total} "
              f"(верно было бы {correct_consumer_total}), "
              f"imbalance={imbalance} — исправляется этапом C "
              f"(валидатор электрического перекрытия, §5.1 ТЗ)")
    finally:
        fx.cleanup()


def test_internal_reset_not_detected_a13():
    """A13: накопитель 100 -> 0 -> 150. Проверка только по границам
    периода (`consumption.py::calculate_from_points`, дельта = end-start,
    consumption.py:96-105) не видит внутренний сброс между известными
    точками и выдаёт заниженный расход с quality='ok'."""
    points = reset_history_points()
    period = Period(ts_from=points[0].timestamp, ts_to=points[-1].timestamp,
                     label="test", description="A13")
    result = calculate_from_points(points, period, device_id="test/reset")

    assert result.quality == "ok", (
        "если это упало — поведение уже изменилось, обновите тест")
    assert result.consumption_kwh == 50.0
    print(f"[REPRODUCED] A13: consumption_kwh={result.consumption_kwh}, "
          f"quality={result.quality!r} — внутренний сброс 100->0->150 не "
          f"обнаружен, т.к. сравниваются только граничные точки периода. "
          f"Исправляется этапом B (§5.4 ТЗ: «Проверять падения между "
          f"соседними точками накопительной энергии, а не только "
          f"end-start»)")


def test_hybrid_gap_swallowed_as_zero_a12():
    """A12: 9 из 10 часовых агрегатов, десятый час вообще отсутствует в
    `period_aggregates` (не просто NULL-строка — строки нет вовсе).
    `AggregateRepo.list_range` (aggregates_repo.py:157-169) возвращает
    только существующие строки, поэтому `ConsumptionService._calculate_hybrid`
    (consumption.py:219-236) даже не узнаёт о пропуске: `any_no_data`
    считает лишь строки с `ap_energy_delta IS NULL`, а не дыры в
    диапазоне. Итог — quality='ok' (не 'gap'!) и ОПРЕДЕЛЁННОЕ число,
    выдающее 90% реального периода за полный расход. Это строже, чем
    формулировка ТЗ §3 ('80% часовых строк'): достаточно и меньшего
    покрытия, если недостающие часы просто отсутствуют как строки."""
    fx = build_legacy_db()
    try:
        meter = fx.meters["tsex"]
        aggregates_repo = AggregateRepo(fx.db)

        # Период ровно из 10 полных часов, выровненный по границе часа —
        # без RPC-хвостов, чтобы не тянуть WbDbClient.
        first_hour = align_hour_down(1_757_400_000)
        for i in range(9):  # десятый час (индекс 9) сознательно пропущен
            start = first_hour + i * 3600
            aggregates_repo.upsert(HourlyAggregate(
                meter_id=meter.id, period_start=start, period_end=start + 3600,
                ap_energy_start=float(i * 10), ap_energy_end=float((i + 1) * 10),
                ap_energy_delta=10.0, p_avg=1000.0, p_max=1200.0,
                samples_count=12, quality_flag="ok", computed_at=start + 3600,
            ))

        period = Period(ts_from=first_hour, ts_to=first_hour + 10 * 3600,
                         label="test", description="A12 hybrid")
        service = ConsumptionService(db_client=None, aggregates_repo=aggregates_repo,
                                      meters_repo=fx.meters_repo)
        result = service.calculate(meter.device_id, period)

        assert result.quality == "ok", (
            "если это упало — поведение уже изменилось, обновите тест: "
            "отсутствующая строка агрегата теперь как-то замечается")
        assert result.consumption_kwh == 90.0, (
            "если это упало — поведение уже изменилось, обновите тест")
        print(f"[REPRODUCED] A12 (хуже, чем в ТЗ): consumption_kwh="
              f"{result.consumption_kwh} выдан с quality={result.quality!r} "
              f"— НИКАКОГО флага о пропуске часа нет вовсе, хотя должно "
              f"быть known_value=90.0 при value=null. Исправляется этапом B "
              f"через accounting_contract.known_sum() и явную проверку "
              f"ожидаемого числа часов, а не только len(list_range()).")
    finally:
        fx.cleanup()


def test_migration_apply_is_not_atomic():
    """§6.1/§11.1.4 ТЗ: `Database._apply_migrations` (db.py:106-126)
    вызывает `executescript(sql)` без явного BEGIN/COMMIT и без записи
    версии только после успеха отдельной транзакцией. Тест показывает:
    при падении миграции ПОСЕРЕДИНЕ файла часть её DDL уже применена (и
    закоммичена — соединение открыто с `isolation_level=None`), а
    строка в schema_migrations не появилась — то самое «половина
    структуры уже новая», которое ТЗ прямо запрещает."""
    tmpdir = tempfile.mkdtemp(prefix="wbem_test_migration_atomic_")
    try:
        db = Database(path=os.path.join(tmpdir, "state.db"))
        db.open()  # применяет реальные 001-005 (005+ уже идёт через
                   # атомарный протокол — см. test_atomic_rollback_actually_rolls_back
                   # ниже; этот тест намеренно бьёт мимо него, напрямую
                   # через executescript, чтобы задокументировать, ПОЧЕМУ
                   # атомарный протокол вообще понадобился)
        assert db.current_schema_version() == 5

        # Имитация гипотетической будущей миграции, применённой СТАРЫМ
        # способом (прямой executescript, без обёртки) — вторая
        # инструкция обязана провалиться (типичная опечатка в ручном DDL).
        broken_sql = (
            "CREATE TABLE hypothetical_probe (id INTEGER PRIMARY KEY, code TEXT);\n"
            "CREATE TABEL hypothetical_probe_2 (id INTEGER PRIMARY KEY);\n"  # опечатка нарочно
        )
        try:
            db.conn().executescript(broken_sql)
            raise AssertionError("ожидалась sqlite3.OperationalError на опечатке")
        except sqlite3.OperationalError:
            pass

        tables = {r["name"] for r in db.conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        # ДЕФЕКТ (у executescript БЕЗ обёртки): первая инструкция уже
        # закоммичена в файл, версия миграции нигде не отмечена —
        # повторный запуск попробует применить тот же файл ещё раз и
        # упадёт на "table already exists".
        assert "hypothetical_probe" in tables, (
            "если это упало — поведение executescript уже изменилось, "
            "обновите тест и docs/migration-plan-v2.md")
        assert db.current_schema_version() == 5  # версия гипотетической миграции нигде не записана

        try:
            db.conn().executescript(
                "CREATE TABLE hypothetical_probe (id INTEGER PRIMARY KEY, code TEXT);\n")
            raise AssertionError("ожидался sqlite3.OperationalError: table already exists")
        except sqlite3.OperationalError as e:
            assert "already exists" in str(e)

        print("[REPRODUCED] §6.1/§11.1.4: частично применённый DDL через "
              "голый executescript остаётся в файле БД, а schema_migrations "
              "об этом не знает — повторный запуск не идемпотентен. Это и "
              "есть причина, по которой миграции >=5 идут через "
              "Database._apply_migration_atomic(), см. следующий тест.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_atomic_migration_wrapper_rolls_back_cleanly():
    """Позитивная проверка исправления: `Database._apply_migration_atomic`
    (db.py) на такой же опечатке откатывает ВСЁ целиком — в отличие от
    голого executescript выше."""
    tmpdir = tempfile.mkdtemp(prefix="wbem_test_migration_atomic_fix_")
    try:
        db = Database(path=os.path.join(tmpdir, "state.db"))
        db.open()
        assert db.current_schema_version() == 5

        broken_sql = (
            "CREATE TABLE hypothetical_probe (id INTEGER PRIMARY KEY, code TEXT);\n"
            "CREATE TABEL hypothetical_probe_2 (id INTEGER PRIMARY KEY);\n"
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (6, 'hypothetical', strftime('%s','now'));\n"
        )
        try:
            db._apply_migration_atomic(6, "hypothetical", broken_sql)
            raise AssertionError("ожидалась ошибка на опечатке")
        except sqlite3.OperationalError:
            pass

        tables = {r["name"] for r in db.conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "hypothetical_probe" not in tables, (
            "атомарный протокол должен откатывать ВСЁ, включая уже "
            "выполненные statements того же скрипта")
        assert db.current_schema_version() == 5, "версия не должна была измениться"

        # Повторный запуск после исправления опечатки проходит штатно —
        # идемпотентность восстановлена.
        fixed_sql = (
            "CREATE TABLE hypothetical_probe (id INTEGER PRIMARY KEY, code TEXT);\n"
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (6, 'hypothetical', strftime('%s','now'));\n"
        )
        db._apply_migration_atomic(6, "hypothetical", fixed_sql)
        assert db.current_schema_version() == 6
        print("[OK] атомарный протокол миграций откатывает частично "
              "применённый DDL целиком и остаётся идемпотентным")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    test_double_counting_a04_reproducible()
    test_internal_reset_not_detected_a13()
    test_hybrid_gap_swallowed_as_zero_a12()
    test_migration_apply_is_not_atomic()
    test_atomic_migration_wrapper_rolls_back_cleanly()
    print("\nВсе воспроизводимые сценарии этапа A подтверждены на "
          "текущем коде v0.11.1.")

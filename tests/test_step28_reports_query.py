"""Тесты Шага 28 (партия 2, задача 3): POST /api/v2/reports/query
(ТЗ §8.4) — срезы отчёта (точка/ветвь/группа/граница баланса) поверх
того же расчётного слоя v2 (accounting_service), что и Обзор и
metrics/query, плюс A21/A22 (режимы состава группы as_was/current и
явное раскрытие отличия состава между периодами сравнения).

Числа во всех тестах посчитаны вручную по фикстурам (не сравнением двух
экранов, использующих одну и ту же функцию) — допуск 0.000001 кВт·ч.

Самостоятельный скрипт (не pytest):
    python tests/test_step28_reports_query.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate
from wb_energy_meter.binding_service import PointBindingRepo

HOUR = 3600
TOL = 1e-6


def approx(a, b, tol=TOL):
    return a is not None and b is not None and abs(a - b) < tol


def make_client():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()

    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)
    registry = MeterRegistry()

    state = _AppState(
        registry=registry, meters_repo=meters_repo, groups_repo=groups_repo,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None,
        consumption_service=None, started_at=time.time(), db=db,
    )
    app = create_app(state)
    return app.test_client(), db, path


def current_rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


class Rig:
    """Общая обвязка: точка + прибор + агрегат(ы) за один вызов, плюс
    регистрация в v2 (та же обвязка, что и в test_step18/test_step27)."""

    def __init__(self, db):
        self.meters = MeterRepo(db, GroupRepo(db))
        self.sources = MeterSourceRepo(db)
        self.points = MeteringPointRepo(db)
        self.aggregates = AggregateRepo(db)
        self.bindings = PointBindingRepo(db)

    def _add_hour(self, meter_id, hour_index, kwh):
        start = hour_index * HOUR
        self.aggregates.upsert(HourlyAggregate(
            meter_id=meter_id, period_start=start, period_end=start + HOUR,
            ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
            p_avg=None, p_max=None, samples_count=1, quality_flag="ok",
            computed_at=start))

    def make_point(self, code, kwh, hour_index=0):
        """Одно значение в час hour_index (по умолчанию час 0)."""
        return self.make_point_at(code, {hour_index: kwh})

    def make_point_at(self, code, hours_kwh):
        """`hours_kwh` — {номер_часа: кВт·ч}, можно несколько часов сразу
        (используется для точки с данными в двух разных периодах)."""
        m = self.meters.add(code, code)
        src = self.sources.open_source(m.id, "wb8-main", code)
        p = self.points.add(code, code)
        self.bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)
        for h, kwh in hours_kwh.items():
            if kwh is not None:
                self._add_hour(m.id, h, kwh)
        return p


# ---------------------------------------------------------------------
# базовые срезы
# ---------------------------------------------------------------------

def test_reports_point_dimension_matches_measured_point():
    """dimension=point: результат — measured_point точки один-в-один,
    member_point_ids = [сама точка]."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p = rig.make_point("P", 12.5)

        r = client.post("/api/v2/reports/query", json={
            "dimension": "point", "scope_ids": [p.id],
            "from": 0, "to": HOUR, "timezone": "UTC",
        })
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["dimension"] == "point"
        row = body["rows"][0]
        assert row["id"] == p.id
        assert row["member_point_ids"] == [p.id]
        assert approx(row["result"]["value"], 12.5)
        assert row["result"]["unit"] == "kWh"
        assert row["result"]["mode"] == "measured"
        assert row["conflict_reason"] is None
        print("[OK] reports/query dimension=point: значение совпадает с measured_point")
    finally:
        db.close(); os.unlink(path)


def test_reports_group_dimension_sum_and_member_ids():
    """dimension=group: сумма состава группы (эффективный состав через
    resolve_effective_members), member_point_ids — фактический состав."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p1 = rig.make_point("G1", 10.0)
        p2 = rig.make_point("G2", 7.0)
        g = client.post("/api/v2/groups", json={"name": "Группа"}).get_json()
        rev = current_rev(client)
        client.post(f"/api/v2/groups/{g['id']}/members",
                    json={"point_id": p1.id, "valid_from": 0, "expected_revision": rev})
        rev = current_rev(client)
        client.post(f"/api/v2/groups/{g['id']}/members",
                    json={"point_id": p2.id, "valid_from": 0, "expected_revision": rev})

        r = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [g["id"]],
            "from": 0, "to": HOUR, "timezone": "UTC",
        })
        assert r.status_code == 200, r.get_json()
        row = r.get_json()["rows"][0]
        assert sorted(row["member_point_ids"]) == sorted([p1.id, p2.id])
        assert approx(row["result"]["value"], 17.0)
        print("[OK] reports/query dimension=group: сумма состава 10+7=17")
    finally:
        db.close(); os.unlink(path)


def test_reports_branch_dimension_no_scope_ids_needed():
    """dimension=branch: scope_ids не требуется — берутся все группы
    верхнего уровня (list_children(None)), как в Обзоре."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p1 = rig.make_point("B1", 21.0)
        p2 = rig.make_point("B2", 9.0)
        ga = client.post("/api/v2/groups", json={"name": "Ветвь А"}).get_json()
        gb = client.post("/api/v2/groups", json={"name": "Ветвь Б"}).get_json()
        client.post(f"/api/v2/groups/{ga['id']}/members",
                    json={"point_id": p1.id, "valid_from": 0, "expected_revision": current_rev(client)})
        client.post(f"/api/v2/groups/{gb['id']}/members",
                    json={"point_id": p2.id, "valid_from": 0, "expected_revision": current_rev(client)})

        r = client.post("/api/v2/reports/query", json={
            "dimension": "branch", "from": 0, "to": HOUR, "timezone": "UTC",
        })
        assert r.status_code == 200, r.get_json()
        rows = r.get_json()["rows"]
        assert len(rows) == 2
        by_name = {row["name"]: row for row in rows}
        assert approx(by_name["Ветвь А"]["result"]["value"], 21.0)
        assert approx(by_name["Ветвь Б"]["result"]["value"], 9.0)
        print("[OK] reports/query dimension=branch: автоматически все ветви верхнего уровня")
    finally:
        db.close(); os.unlink(path)


def test_reports_balance_scope_dimension_imbalance():
    """dimension=balance_scope: небаланс = вход - выход, member_point_ids
    объединяет вход и выход."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_in = rig.make_point("IN", 100.0)
        p_out = rig.make_point("OUT", 70.0)
        scope = client.post("/api/v2/balance-scopes", json={
            "name": "Баланс", "input_point_ids": [p_in.id], "output_point_ids": [p_out.id],
        }).get_json()

        r = client.post("/api/v2/reports/query", json={
            "dimension": "balance_scope", "scope_ids": [scope["id"]],
            "from": 0, "to": HOUR, "timezone": "UTC",
        })
        assert r.status_code == 200, r.get_json()
        row = r.get_json()["rows"][0]
        assert sorted(row["member_point_ids"]) == sorted([p_in.id, p_out.id])
        assert approx(row["result"]["value"], 30.0)
        assert row["result"]["mode"] == "balance"
        print("[OK] reports/query dimension=balance_scope: небаланс 100-70=30")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A04 — конфликт на уровне строки, не всей выгрузки
# ---------------------------------------------------------------------

def test_a04_group_dimension_conflict_is_row_level_not_request_level():
    """Подтверждённое электрическое пересечение внутри ОДНОЙ группы не
    должно валить всю выгрузку — только эта строка получает
    conflict_reason и result=None, соседняя нормальная группа считается
    как обычно (в отличие от metrics/query mode=sum, где такой же
    конфликт возвращает 409 на весь запрос)."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_ok = rig.make_point("OK", 5.0)
        p_top = rig.make_point("TOP", 40.0)
        p_child = rig.make_point("CHILD", 15.0)

        n_src = client.post("/api/v2/topology/nodes",
                             json={"code": "S", "name": "Ввод", "kind": "source"}).get_json()
        n_top = client.post("/api/v2/topology/nodes",
                             json={"code": "T", "name": "Узел верх", "kind": "panel"}).get_json()
        n_child = client.post("/api/v2/topology/nodes",
                               json={"code": "C", "name": "Узел низ", "kind": "load"}).get_json()
        e1 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_src["id"], "to_node_id": n_top["id"], "primary_point_id": p_top.id
        }).get_json()
        e2 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_top["id"], "to_node_id": n_child["id"], "primary_point_id": p_child.id
        }).get_json()
        rev = current_rev(client)
        pub = client.post("/api/v2/topology/publish",
                           json={"edge_ids": [e1["id"], e2["id"]],
                                 "expected_configuration_revision": rev})
        assert pub.status_code == 200, pub.get_json()

        g_ok = client.post("/api/v2/groups", json={"name": "Нормальная"}).get_json()
        g_bad = client.post("/api/v2/groups", json={"name": "Пересечение"}).get_json()
        client.post(f"/api/v2/groups/{g_ok['id']}/members",
                    json={"point_id": p_ok.id, "valid_from": 0, "expected_revision": current_rev(client)})
        client.post(f"/api/v2/groups/{g_bad['id']}/members",
                    json={"point_id": p_top.id, "valid_from": 0, "expected_revision": current_rev(client)})
        client.post(f"/api/v2/groups/{g_bad['id']}/members",
                    json={"point_id": p_child.id, "valid_from": 0, "expected_revision": current_rev(client)})

        r = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [g_ok["id"], g_bad["id"]],
            "from": 0, "to": HOUR, "timezone": "UTC",
        })
        assert r.status_code == 200, r.get_json()
        rows = {row["id"]: row for row in r.get_json()["rows"]}
        assert approx(rows[g_ok["id"]]["result"]["value"], 5.0)
        assert rows[g_ok["id"]]["conflict_reason"] is None
        assert rows[g_bad["id"]]["result"] is None
        assert rows[g_bad["id"]]["conflict_reason"] is not None
        print("[OK] A04: конфликт пересечения — только своя строка, запрос целиком не падает")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A21/A22 — режимы состава группы as_was / current
# ---------------------------------------------------------------------

def _setup_tenant_move(client, db):
    """X и Y в "Арендатор А" с часа 0; в момент M=5ч X уходит в
    "Арендатор Б". У X и Y есть показания и в первом периоде (час 0),
    и во втором (час 10) — можно строить отчёт как за период ДО
    переезда, так и ПОСЛЕ."""
    rig = Rig(db)
    p_x = rig.make_point_at("X", {0: 40.0, 10: 25.0})
    p_y = rig.make_point_at("Y", {0: 15.0, 10: 18.0})

    tenant_a = client.post("/api/v2/groups", json={"name": "Арендатор А"}).get_json()
    tenant_b = client.post("/api/v2/groups", json={"name": "Арендатор Б"}).get_json()

    client.post(f"/api/v2/groups/{tenant_a['id']}/members",
                json={"point_id": p_y.id, "valid_from": 0,
                      "expected_revision": current_rev(client)})
    client.post(f"/api/v2/groups/{tenant_a['id']}/members",
                json={"point_id": p_x.id, "valid_from": 0,
                      "expected_revision": current_rev(client)})

    move_at = 5 * HOUR
    r = client.delete(
        f"/api/v2/groups/{tenant_a['id']}/members/{p_x.id}",
        query_string={"at": move_at, "expected_revision": current_rev(client)})
    assert r.status_code == 204, r.get_json()
    client.post(f"/api/v2/groups/{tenant_b['id']}/members",
                json={"point_id": p_x.id, "valid_from": move_at,
                      "expected_revision": current_rev(client)})
    return p_x, p_y, tenant_a, tenant_b, move_at


def test_a21_composition_mode_as_was_keeps_past_report_stable():
    """A21: "перенос точки из арендатора А в Б с 15 числа — отчёт за
    прошлый месяц не меняется". composition_mode=as_was (по умолчанию)
    резолвит состав группы НА МОМЕНТ начала запрошенного периода —
    отчёт за период ДО переезда видит X ещё в А, независимо от того,
    что случилось после."""
    client, db, path = make_client()
    try:
        p_x, p_y, tenant_a, tenant_b, move_at = _setup_tenant_move(client, db)

        # период ДО переезда (час 0): as_was должен включать X и Y.
        r = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [tenant_a["id"]],
            "from": 0, "to": HOUR, "timezone": "UTC",
        })
        row = r.get_json()["rows"][0]
        assert sorted(row["member_point_ids"]) == sorted([p_x.id, p_y.id])
        assert approx(row["result"]["value"], 55.0)  # 40 + 15
        assert r.get_json()["composition_mode"] == "as_was"

        # тот же период, но composition_mode=current — X уже выехал "на
        # сейчас", в А остаётся только Y. Число (15) СОЗНАТЕЛЬНО другое —
        # это другой, явно подписанный режим, а не тот же прошлый отчёт.
        r2 = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [tenant_a["id"]],
            "from": 0, "to": HOUR, "timezone": "UTC", "composition_mode": "current",
        })
        row2 = r2.get_json()["rows"][0]
        assert row2["member_point_ids"] == [p_y.id]
        assert approx(row2["result"]["value"], 15.0)
        print("[OK] A21: as_was (55=40+15) стабилен для прошлого периода; "
              "current (15) — другой явно подписанный режим")
    finally:
        db.close(); os.unlink(path)


def test_a22_current_mode_uses_historically_correct_meter_data():
    """A22: "текущие группы, но исторически правильные физические
    приборы". В режиме current состав "Арендатор Б" на сейчас уже
    включает X — но число для СТАРОГО периода (час 0, ДО переезда)
    всё равно берётся из настоящих исторических показаний X (40), а
    не из чего-то другого."""
    client, db, path = make_client()
    try:
        p_x, p_y, tenant_a, tenant_b, move_at = _setup_tenant_move(client, db)

        # "Арендатор Б" as_was на час 0: X ещё не в Б -> пусто -> нет данных.
        r_as_was = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [tenant_b["id"]],
            "from": 0, "to": HOUR, "timezone": "UTC",
        })
        row_as_was = r_as_was.get_json()["rows"][0]
        assert row_as_was["member_point_ids"] == []
        assert row_as_was["result"] is None
        assert row_as_was["conflict_reason"] is None  # A10: нет состава != конфликт

        # "Арендатор Б" current на тот же старый час 0: состав "на сейчас"
        # (X уже в Б), но значение X за час 0 — исторически верное (40),
        # то самое, что было у X ДО переезда.
        r_current = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [tenant_b["id"]],
            "from": 0, "to": HOUR, "timezone": "UTC", "composition_mode": "current",
        })
        row_current = r_current.get_json()["rows"][0]
        assert row_current["member_point_ids"] == [p_x.id]
        assert approx(row_current["result"]["value"], 40.0)
        print("[OK] A22: current — состав на сейчас, но число за старый период "
              "исторически верное (40, показание X до переезда)")
    finally:
        db.close(); os.unlink(path)


def test_composition_changed_flag_revealed_across_tenant_move():
    """Сравнение периодов ДО/ПОСЛЕ переезда для "Арендатора А" обязано
    явно показать composition_changed=True (состав отличается), а не
    молча отдать две несравнимые суммы рядом."""
    client, db, path = make_client()
    try:
        p_x, p_y, tenant_a, tenant_b, move_at = _setup_tenant_move(client, db)

        r = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [tenant_a["id"]],
            "from": 10 * HOUR, "to": 11 * HOUR, "timezone": "UTC",  # ПОСЛЕ переезда
            "compare": {"from": 0, "to": HOUR},  # ДО переезда
        })
        assert r.status_code == 200, r.get_json()
        row = r.get_json()["rows"][0]

        assert row["member_point_ids"] == [p_y.id]
        assert approx(row["result"]["value"], 18.0)
        assert sorted(row["compare_member_point_ids"]) == sorted([p_x.id, p_y.id])
        assert approx(row["compare_result"]["value"], 55.0)
        assert row["composition_changed"] is True

        assert approx(row["delta_value"], 18.0 - 55.0)
        # 55 -> база сравнения (compare_result), delta% = -37/55*100
        assert approx(row["delta_percentage"], round(-37.0 / 55.0 * 100, 2), tol=0.01)
        assert row["delta_percentage_reason"] is None

        # без переезда состав не отличается -> composition_changed=False.
        r_stable = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [tenant_b["id"]],
            "from": 10 * HOUR, "to": 11 * HOUR, "timezone": "UTC",
            "compare": {"from": 10 * HOUR, "to": 11 * HOUR},
        })
        row_stable = r_stable.get_json()["rows"][0]
        assert row_stable["composition_changed"] is False
        print("[OK] A21/A22: composition_changed=True при переезде, "
              "delta_value/delta_percentage посчитаны корректно")
    finally:
        db.close(); os.unlink(path)


def test_delta_percentage_no_division_by_zero_or_missing_base():
    """A11-подобная защита: delta_percentage — null с явной причиной,
    когда база сравнения (значение периода-компаратора) 0 или
    неизвестна — никогда не деление на ноль/фиктивный процент."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_z = rig.make_point_at("Z", {0: 0.0, 10: 12.0})  # база=0 в компараторе

        r = client.post("/api/v2/reports/query", json={
            "dimension": "point", "scope_ids": [p_z.id],
            "from": 10 * HOUR, "to": 11 * HOUR, "timezone": "UTC",
            "compare": {"from": 0, "to": HOUR},
        })
        row = r.get_json()["rows"][0]
        assert approx(row["result"]["value"], 12.0)
        assert approx(row["compare_result"]["value"], 0.0)
        assert approx(row["delta_value"], 12.0)
        assert row["delta_percentage"] is None
        assert row["delta_percentage_reason"] == "zero_or_negative_base"
        print("[OK] delta_percentage=null с причиной при нулевой базе сравнения "
              "(без фиктивного процента и без деления на ноль)")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# валидация / ревизия
# ---------------------------------------------------------------------

def test_reports_query_validation_errors():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p = rig.make_point("P", 1.0)

        r = client.post("/api/v2/reports/query",
                         json={"dimension": "bogus", "from": 0, "to": HOUR})
        assert r.status_code == 400, r.get_json()

        r = client.post("/api/v2/reports/query",
                         json={"dimension": "group", "from": 0, "to": HOUR})
        assert r.status_code == 400, r.get_json()

        r = client.post("/api/v2/reports/query", json={
            "dimension": "point", "scope_ids": [999999], "from": 0, "to": HOUR})
        assert r.status_code == 404, r.get_json()

        r = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [999999], "from": 0, "to": HOUR})
        assert r.status_code == 404, r.get_json()

        r = client.post("/api/v2/reports/query", json={
            "dimension": "balance_scope", "scope_ids": [999999], "from": 0, "to": HOUR})
        assert r.status_code == 404, r.get_json()

        r = client.post("/api/v2/reports/query", json={
            "dimension": "point", "scope_ids": [p.id], "from": 0, "to": HOUR,
            "composition_mode": "bogus"})
        assert r.status_code == 400, r.get_json()
        print("[OK] reports/query: 400/404 на неверные dimension/scope_ids/composition_mode")
    finally:
        db.close(); os.unlink(path)


def test_reports_query_pins_revision_and_rejects_unknown():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        rig.make_point("P", 1.0)
        rev = current_rev(client)

        r = client.post("/api/v2/reports/query",
                         json={"dimension": "branch", "from": 0, "to": HOUR})
        assert r.get_json()["configuration_revision_id"] == rev

        r2 = client.post("/api/v2/reports/query", json={
            "dimension": "branch", "from": 0, "to": HOUR,
            "configuration_revision_id": rev + 999})
        assert r2.status_code == 404, r2.get_json()
        print("[OK] reports/query: ревизия фиксируется в ответе; неизвестная -> 404")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A45 — CSV formula-injection guard во фронтенде (csvCell)
# ---------------------------------------------------------------------

def test_a45_csv_cell_neutralizes_formula_prefixes():
    """Поведенческая (не только текстовая) проверка исправленного
    csvCell() в static/index.html: значения, начинающиеся с =, +, -, @,
    экспортируются как безопасный текст (с ведущей "'"), обычный текст и
    отрицательное число небаланса не искажаются. Требует системный node
    (уже используется в проекте для статической проверки "node --check"
    инлайновых скриптов) — если node недоступен в песочнице, тест
    пропускается, а не падает (тот же принцип, что и для тестов демона,
    требующих mosquitto)."""
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("[SKIP] A45 csvCell: node недоступен в этой песочнице")
        return

    index_path = os.path.join(REPO_ROOT, "wb_energy_meter", "static", "index.html")
    with open(index_path, encoding="utf-8") as f:
        html = f.read()
    start = html.index("csvCell(v){")
    end = html.index("},", start) + 1
    fn_src = html[start:end]
    assert "^[=+\\-@\\t\\r]" in fn_src, "A45-guard (regex префиксов формулы) не найден в csvCell"

    js = (
        "const obj = {" + fn_src + "};\n"
        "const cases = ['=SUM(A1:A9)', '+1234', '-описание', '@cmd', "
        "'обычный текст', null, '-12.345', 'name;with;semicolon'];\n"
        "console.log(JSON.stringify(cases.map(c => obj.csvCell(c))));\n"
    )
    result = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    import json as _json
    out = _json.loads(result.stdout)

    assert out[0] == "'=SUM(A1:A9)"
    assert out[1] == "'+1234"
    assert out[2] == "'-описание"
    assert out[3] == "'@cmd"
    assert out[4] == "обычный текст"
    assert out[5] == ""
    # A45 требует нейтрализации значений, похожих на формулу, ДАЖЕ если
    # это число-как-текст — но в реальном коде отчётов числовой
    # отрицательный небаланс НИКОГДА не передаётся через csvCell (он
    # пишется в CSV как есть отдельной веткой, см. downloadCompare/
    # downloadBalance) — здесь только подтверждаем, что сама функция,
    # если ей всё же передать такую строку, не роняет и не портит кавычки.
    assert out[6] == "'-12.345"
    assert out[7] == '"name;with;semicolon"'
    print("[OK] A45: csvCell нейтрализует префиксы формул (=+-@), "
          "обычный текст и null не искажены")


if __name__ == "__main__":
    test_reports_point_dimension_matches_measured_point()
    test_reports_group_dimension_sum_and_member_ids()
    test_reports_branch_dimension_no_scope_ids_needed()
    test_reports_balance_scope_dimension_imbalance()
    test_a04_group_dimension_conflict_is_row_level_not_request_level()
    test_a21_composition_mode_as_was_keeps_past_report_stable()
    test_a22_current_mode_uses_historically_correct_meter_data()
    test_composition_changed_flag_revealed_across_tenant_move()
    test_delta_percentage_no_division_by_zero_or_missing_base()
    test_reports_query_validation_errors()
    test_reports_query_pins_revision_and_rejects_unknown()
    test_a45_csv_cell_neutralizes_formula_prefixes()
    print("[ALL OK] test_step28_reports_query")

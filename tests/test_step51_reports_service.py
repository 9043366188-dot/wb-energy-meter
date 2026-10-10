"""Тесты Шага 51 (партия 11, этап 11.4, F1): расчёт срезов отчёта в
`wb_energy_meter/reports_service.py` — НАПРЯМУЮ, без HTTP. Обработчик
`POST /api/v2/reports/query` (api_v2.py) теперь только разбирает тело,
фиксирует ревизию и переводит исключения в HTTP; весь расчёт строк живёт
в сервисе. Поведение ответа не изменилось ни в одном байте — это
проверено отдельно (сверка до/после на нагрузочной базе, см. PR) и
существующими `test_step28`, `test_step40` (T11), `tests/browser/test_b03`.

Ожидаемые числа посчитаны вручную до запуска:

  S1  точка P=12.5 (час 0): строка point = 12.5, member_point_ids=[P];
      неизвестная точка → ReportTargetNotFound(ids=[999])
  S2  X=40, Y=15 в «Арендатор А» с 0; X → «Арендатор Б» с 5 ч.
      А as_was [0,1ч) = 55 (X,Y); А current = 15 (только Y);
      Б as_was = нет состава (result=None, conflict=None);
      Б current = 40; неизвестная группа → ReportTargetNotFound
  S3  A=100 (ввод), D=20 под A; группа {A,D} → строка с conflict_reason и
      result=None; соседняя группа {C=30} считается как обычно (30)
  S4  сравнение периодов для А: основной период (10 ч) Y=18, база (0 ч)
      X+Y=55 → composition_changed=True, delta_value=−37,
      delta_percentage=round(−37/55·100, 2)=−67.27; Б, период без данных
      у базы (результат 0 в базе) — процент null с причиной
  S5  граница баланса: вход 100, выходы 70+40 → небаланс −10;
      member_point_ids = вход + выходы
  S6  dimension=branch: ветви уровня 1 сети, id строки = id линии,
      значения B=60, C=30
  S7  tag_result вызывается ровно по разу на каждый непустой результат
      (основной + сравнение) с зафиксированной ревизией
  S8  общий cache заполняется и не растёт при повторном расчёте
  S9  хелперы: resolve_group_points(as_was/current), row_result для
      пустого состава и «точка не найдена»
  S10 строки сервиса совпадают со строками HTTP-ответа (кроме as_of)

Самостоятельный скрипт (не pytest):
    python tests/test_step51_reports_service.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import reports_service
from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate
from wb_energy_meter.binding_service import PointBindingRepo
from wb_energy_meter.group_repo_v2 import GroupRepoV2
from wb_energy_meter.topology_service import ElectricalEdgeRepo, ElectricalNodeRepo

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
    state = _AppState(
        registry=MeterRegistry(), meters_repo=meters_repo, groups_repo=groups_repo,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None,
        consumption_service=None, started_at=time.time(), db=db,
    )
    return create_app(state).test_client(), db, path


def current_rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


class Rig:
    def __init__(self, db):
        self.db = db
        self.meters = MeterRepo(db, GroupRepo(db))
        self.sources = MeterSourceRepo(db)
        self.points = MeteringPointRepo(db)
        self.aggregates = AggregateRepo(db)
        self.bindings = PointBindingRepo(db)
        self.edges = ElectricalEdgeRepo(db)
        self.nodes = ElectricalNodeRepo(db)
        self.groups = GroupRepoV2(db)

    def make_point_at(self, code, hours_kwh):
        m = self.meters.add(code, code)
        src = self.sources.open_source(m.id, "wb8-main", code)
        p = self.points.add(code, code)
        self.bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)
        for h, kwh in hours_kwh.items():
            self.aggregates.upsert(HourlyAggregate(
                meter_id=m.id, period_start=h * HOUR, period_end=(h + 1) * HOUR,
                ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
                p_avg=None, p_max=None, samples_count=1, quality_flag="ok",
                computed_at=0))
        return p

    def repos(self):
        return dict(
            binding_repo=self.bindings, aggregates_repo=self.aggregates,
            source_repo=self.sources, edge_repo=self.edges, node_repo=self.nodes,
            group_repo=self.groups, point_repo=self.points)

    def rows(self, dimension, scope_ids=None, t_from=0, t_to=HOUR, mode="as_was",
             compare=None, tag=None, cache=None, revision=7):
        tagged = [] if tag is None else tag
        with self.db.read() as c:
            return reports_service.build_rows(
                c, db=self.db, dimension=dimension, scope_ids=scope_ids,
                ts_from=t_from, ts_to=t_to, timezone_name="UTC",
                composition_mode=mode, compare_range=compare,
                pinned_revision=revision,
                tag_result=lambda r, rev: (tagged.append((id(r), rev)), r)[1],
                cache=cache, **self.repos())


def tenant_move(client, rig):
    """X=40 (час 0) / 25 (час 10), Y=15 / 18; X в «А» с 0 и до 5 ч, затем в «Б»."""
    p_x = rig.make_point_at("X", {0: 40.0, 10: 25.0})
    p_y = rig.make_point_at("Y", {0: 15.0, 10: 18.0})
    ta = client.post("/api/v2/groups", json={"name": "Арендатор А"}).get_json()
    tb = client.post("/api/v2/groups", json={"name": "Арендатор Б"}).get_json()
    for pid in (p_y.id, p_x.id):
        client.post(f"/api/v2/groups/{ta['id']}/members",
                    json={"point_id": pid, "valid_from": 0,
                          "expected_revision": current_rev(client)})
    move_at = 5 * HOUR
    r = client.delete(f"/api/v2/groups/{ta['id']}/members/{p_x.id}",
                      query_string={"at": move_at, "expected_revision": current_rev(client)})
    assert r.status_code == 204, r.get_json()
    client.post(f"/api/v2/groups/{tb['id']}/members",
                json={"point_id": p_x.id, "valid_from": move_at,
                      "expected_revision": current_rev(client)})
    return p_x, p_y, ta["id"], tb["id"]


def make_node(client, code, name, kind):
    r = client.post("/api/v2/topology/nodes", json={"code": code, "name": name, "kind": kind})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def make_edge(client, a, b, point=None):
    body = {"from_node_id": a["id"], "to_node_id": b["id"]}
    if point is not None:
        body["primary_point_id"] = point.id
    r = client.post("/api/v2/topology/edges", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def publish(client, edges):
    r = client.post("/api/v2/topology/publish", json={
        "edge_ids": [e["id"] for e in edges],
        "expected_configuration_revision": current_rev(client)})
    assert r.status_code == 200, r.get_json()


# ---------------------------------------------------------------------

def test_s1_point_rows_and_unknown_point():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p = rig.make_point_at("P", {0: 12.5})
        rows = rig.rows("point", [p.id])
        assert len(rows) == 1
        row = rows[0]
        assert row["dimension"] == "point" and row["id"] == p.id and row["name"] == "P"
        assert row["member_point_ids"] == [p.id]
        assert approx(row["result"]["value"], 12.5), row["result"]
        assert row["conflict_reason"] is None
        assert "compare_result" not in row  # без compare полей сравнения нет

        try:
            rig.rows("point", [p.id, 999])
        except reports_service.ReportTargetNotFound as e:
            assert e.ids == [999] and "999" in str(e), (e.ids, str(e))
        else:
            raise AssertionError("ожидался ReportTargetNotFound")
        print("[OK] S1: point — 12.5, member_point_ids=[P]; неизвестная точка → "
              "ReportTargetNotFound(ids=[999])")
    finally:
        db.close(); os.unlink(path)


def test_s2_group_modes_and_unknown_group():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_x, p_y, ta, tb = tenant_move(client, rig)

        a_as_was = rig.rows("group", [ta], mode="as_was")[0]
        assert sorted(a_as_was["member_point_ids"]) == sorted([p_x.id, p_y.id])
        assert approx(a_as_was["result"]["value"], 55.0)

        a_cur = rig.rows("group", [ta], mode="current")[0]
        assert a_cur["member_point_ids"] == [p_y.id]
        assert approx(a_cur["result"]["value"], 15.0)

        b_as_was = rig.rows("group", [tb], mode="as_was")[0]
        assert b_as_was["member_point_ids"] == [] and b_as_was["result"] is None
        assert b_as_was["conflict_reason"] is None

        b_cur = rig.rows("group", [tb], mode="current")[0]
        assert b_cur["member_point_ids"] == [p_x.id]
        assert approx(b_cur["result"]["value"], 40.0)

        try:
            rig.rows("group", [ta, 4242])
        except reports_service.ReportTargetNotFound as e:
            assert e.ids == [4242]
        else:
            raise AssertionError("ожидался ReportTargetNotFound")
        print("[OK] S2: А as_was=55, А current=15, Б as_was=нет состава (None), "
              "Б current=40; неизвестная группа → ReportTargetNotFound")
    finally:
        db.close(); os.unlink(path)


def test_s3_overlap_conflict_is_row_level():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_a = rig.make_point_at("A", {0: 100.0})
        p_d = rig.make_point_at("D", {0: 20.0})
        p_c = rig.make_point_at("C", {0: 30.0})
        src = make_node(client, "SRC", "Ввод", "source")
        n_a = make_node(client, "NA", "A", "panel")
        n_d = make_node(client, "ND", "D", "load")
        publish(client, [make_edge(client, src, n_a, p_a), make_edge(client, n_a, n_d, p_d)])

        mixed = client.post("/api/v2/groups", json={"name": "Смешанная"}).get_json()["id"]
        other = client.post("/api/v2/groups", json={"name": "Отдельная"}).get_json()["id"]
        for gid, pid in ((mixed, p_a.id), (mixed, p_d.id), (other, p_c.id)):
            client.post(f"/api/v2/groups/{gid}/members", json={
                "point_id": pid, "valid_from": 0, "expected_revision": current_rev(client)})

        rows = {r["id"]: r for r in rig.rows("group", [mixed, other], mode="current")}
        assert rows[mixed]["result"] is None and rows[mixed]["conflict_reason"], rows[mixed]
        assert "путь от узла" in rows[mixed]["conflict_reason"], rows[mixed]["conflict_reason"]
        assert approx(rows[other]["result"]["value"], 30.0), rows[other]
        assert rows[other]["conflict_reason"] is None
        print("[OK] S3: пересечение A+D — conflict_reason у ОДНОЙ строки (result=None), "
              "соседняя группа {C} = 30 считается как обычно")
    finally:
        db.close(); os.unlink(path)


def test_s4_compare_composition_changed_and_delta_percentage():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_x, p_y, ta, tb = tenant_move(client, rig)

        row = rig.rows("group", [ta], t_from=10 * HOUR, t_to=11 * HOUR,
                       compare=(0, HOUR))[0]
        assert row["member_point_ids"] == [p_y.id]
        assert approx(row["result"]["value"], 18.0)
        assert sorted(row["compare_member_point_ids"]) == sorted([p_x.id, p_y.id])
        assert approx(row["compare_result"]["value"], 55.0)
        assert row["composition_changed"] is True
        assert approx(row["delta_value"], -37.0)
        assert approx(row["delta_percentage"], -67.27, tol=0.01), row["delta_percentage"]
        assert row["delta_percentage_reason"] is None
        assert row["compare_conflict_reason"] is None

        # режим current: состав в обоих периодах «на сейчас» — не изменился
        row_cur = rig.rows("group", [ta], t_from=10 * HOUR, t_to=11 * HOUR,
                           compare=(0, HOUR), mode="current")[0]
        assert row_cur["composition_changed"] is False
        assert row_cur["member_point_ids"] == row_cur["compare_member_point_ids"] == [p_y.id]

        # база сравнения с нулём: процента нет, причина названа
        p_z = rig.make_point_at("Z", {0: 0.0, 3: 9.0})
        row_z = rig.rows("point", [p_z.id], t_from=3 * HOUR, t_to=4 * HOUR,
                         compare=(0, HOUR))[0]
        assert approx(row_z["result"]["value"], 9.0)
        assert approx(row_z["compare_result"]["value"], 0.0)
        assert approx(row_z["delta_value"], 9.0)
        assert row_z["delta_percentage"] is None and row_z["delta_percentage_reason"]
        assert row_z["composition_changed"] is False
        print("[OK] S4: А после/до переезда 18 vs 55 → composition_changed, delta −37, "
              "−67.27 %; current — состав не менялся; нулевая база → процент null с причиной")
    finally:
        db.close(); os.unlink(path)


def test_s5_balance_scope_rows():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_in = rig.make_point_at("in", {0: 100.0})
        p_o1 = rig.make_point_at("o1", {0: 70.0})
        p_o2 = rig.make_point_at("o2", {0: 40.0})
        with db.transaction() as c:
            c.execute("INSERT INTO balance_scopes (id, name, created_at, updated_at) "
                      "VALUES (1, 'Объект', 0, 0)")
            for pid, side in ((p_in.id, "input"), (p_o1.id, "output"), (p_o2.id, "output")):
                c.execute("INSERT INTO balance_members (scope_id, point_id, side, valid_from, "
                          "created_at) VALUES (1, ?, ?, 0, 0)", (pid, side))
        row = rig.rows("balance_scope", [1])[0]
        assert row["name"] == "Объект" and row["conflict_reason"] is None
        assert approx(row["result"]["value"], -10.0), row["result"]
        assert row["member_point_ids"] == [p_in.id, p_o1.id, p_o2.id], row["member_point_ids"]

        try:
            rig.rows("balance_scope", [1, 77])
        except reports_service.ReportTargetNotFound as e:
            assert e.ids == [77]
        else:
            raise AssertionError("ожидался ReportTargetNotFound")
        print("[OK] S5: граница баланса: небаланс −10, member_point_ids = вход + выходы; "
              "неизвестная граница → ReportTargetNotFound")
    finally:
        db.close(); os.unlink(path)


def test_s6_branch_rows_are_level1_network_branches():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_a = rig.make_point_at("A", {0: 100.0})
        p_b = rig.make_point_at("B", {0: 60.0})
        p_c = rig.make_point_at("C", {0: 30.0})
        src = make_node(client, "SRC", "Ввод", "source")
        n_top = make_node(client, "TOP", "ГРЩ", "panel")
        n_b = make_node(client, "NB", "Цех", "load")
        n_c = make_node(client, "NC", "Сервер", "load")
        e_a = make_edge(client, src, n_top, p_a)
        e_b = make_edge(client, n_top, n_b, p_b)
        e_c = make_edge(client, n_top, n_c, p_c)
        publish(client, [e_a, e_b, e_c])

        rows = rig.rows("branch", None)
        by_name = {r["name"]: r for r in rows}
        assert set(by_name) == {"Цех", "Сервер"}, list(by_name)
        assert by_name["Цех"]["id"] == e_b["id"] and by_name["Сервер"]["id"] == e_c["id"]
        assert approx(by_name["Цех"]["result"]["value"], 60.0)
        assert approx(by_name["Сервер"]["result"]["value"], 30.0)
        assert by_name["Цех"]["member_point_ids"] == [p_b.id]
        print("[OK] S6: branch — «Цех»=60, «Сервер»=30 (id строки = id линии), без scope_ids")
    finally:
        db.close(); os.unlink(path)


def test_s7_tag_result_called_once_per_result_with_pinned_revision():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_x, p_y, ta, tb = tenant_move(client, rig)
        tagged = []
        rows = rig.rows("group", [ta, tb], t_from=10 * HOUR, t_to=11 * HOUR,
                        compare=(0, HOUR), mode="as_was", tag=tagged, revision=31)
        # А: основной + сравнение; Б as_was: основной период (10 ч) — X в Б (25),
        # сравнение (0 ч) — состава нет → result None, не тегируется
        expected = sum(1 for r in rows if r["result"] is not None) + \
            sum(1 for r in rows if r["compare_result"] is not None)
        assert expected == 3, expected
        assert len(tagged) == expected, (len(tagged), expected)
        assert {rev for _i, rev in tagged} == {31}
        print("[OK] S7: tag_result вызван по разу на каждый непустой результат "
              f"({expected}) с ревизией 31")
    finally:
        db.close(); os.unlink(path)


def test_s8_shared_cache_filled_and_stable():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_x, p_y, ta, tb = tenant_move(client, rig)
        cache = {}
        rig.rows("group", [ta, tb], mode="current", cache=cache)
        n = len(cache)
        assert n > 0, "кэш measured_point не заполнен"
        first = rig.rows("group", [ta, tb], mode="current", cache=cache)
        assert len(cache) == n, (len(cache), n)
        second = rig.rows("group", [ta, tb], mode="current", cache=None)
        strip = lambda rows: [(r["id"], r["member_point_ids"], r["result"]["value"]
                               if r["result"] else None) for r in rows]
        assert strip(first) == strip(second)
        print(f"[OK] S8: общий cache заполнен ({n} ключей), не растёт при повторе, "
              "значения с кэшем и без него совпадают")
    finally:
        db.close(); os.unlink(path)


def test_s9_helpers():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_x, p_y, ta, tb = tenant_move(client, rig)
        g = rig.groups
        assert sorted(reports_service.resolve_group_points(g, ta, "as_was", 0)) == \
            sorted([p_x.id, p_y.id])
        assert reports_service.resolve_group_points(g, ta, "current", 0) == [p_y.id]
        assert reports_service.resolve_group_points(g, tb, "as_was", 0) == []

        args = (rig.bindings, rig.aggregates, rig.sources, rig.edges, 0, HOUR, "UTC")
        with db.read():
            assert reports_service.row_result("point", [], *args) == (None, "точка не найдена")
            assert reports_service.row_result("group", [], *args) == (None, None)
            res, why = reports_service.row_result("point", [p_y.id], *args)
            assert why is None and approx(res.value, 15.0)
        print("[OK] S9: resolve_group_points as_was/current; row_result — пустой состав "
              "и «точка не найдена»")
    finally:
        db.close(); os.unlink(path)


def test_s10_service_rows_equal_http_rows():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_x, p_y, ta, tb = tenant_move(client, rig)

        def strip_as_of(obj):
            if isinstance(obj, dict):
                return {k: strip_as_of(v) for k, v in obj.items() if k != "as_of"}
            if isinstance(obj, list):
                return [strip_as_of(v) for v in obj]
            return obj

        r = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [ta, tb], "from": 10 * HOUR,
            "to": 11 * HOUR, "timezone": "UTC", "composition_mode": "as_was",
            "compare": {"from": 0, "to": HOUR}})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        rev = body["configuration_revision_id"]
        rows = rig.rows("group", [ta, tb], t_from=10 * HOUR, t_to=11 * HOUR,
                        compare=(0, HOUR), revision=rev,
                        tag=None)
        # результаты из сервиса тегируются заглушкой, поэтому сверяем без
        # полей ревизии/as_of, которые проставляет HTTP-слой
        def drop_rev(o):
            o = strip_as_of(o)
            if isinstance(o, dict):
                return {k: drop_rev(v) for k, v in o.items()
                        if k not in ("configuration_revision_id", "configuration_revision_ids")}
            if isinstance(o, list):
                return [drop_rev(v) for v in o]
            return o
        assert drop_rev(rows) == drop_rev(body["rows"])
        print("[OK] S10: строки сервиса совпадают со строками HTTP-ответа "
              "(кроме as_of и полей ревизии)")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_s1_point_rows_and_unknown_point()
    test_s2_group_modes_and_unknown_group()
    test_s3_overlap_conflict_is_row_level()
    test_s4_compare_composition_changed_and_delta_percentage()
    test_s5_balance_scope_rows()
    test_s6_branch_rows_are_level1_network_branches()
    test_s7_tag_result_called_once_per_result_with_pinned_revision()
    test_s8_shared_cache_filled_and_stable()
    test_s9_helpers()
    test_s10_service_rows_equal_http_rows()
    print("\nВсе тесты reports_service (Шаг 51, партия 11, F1) пройдены.")

#!/usr/bin/env python3
"""Партия 12, этап 12.4: A07 — «состав есть, топология неизвестна».

Ожидание §13 большого ТЗ: система показывает сумму состава со статусом
`unverified`, но не выдаёт её за подтверждённый итог объекта и не строит по ней
процентное распределение. Раньше проверялось только на уровне расчётного ядра
(`test_step17_accounting_service.py::test_unverified_topology_marks_structure_quality`),
а «Обзор» и `reports_service` не проверялись.

Числа посчитаны вручную:
  Сценарий 1 (сети нет вообще): P1=5, P2=7 в группе G. Обзор: итога объекта нет,
    у группы сумма 12 с structure_quality=unverified и БЕЗ процента; отчёт по группе —
    structure_confirmed=false; процент изменения при сравнении периодов — не распределение, остаётся.
  Сценарий 2 (сеть есть, группа с точкой вне сети): ввод MA=100 → щит → MB=60;
    MC=10 вне сети. Итог объекта 100 (verified). Группа Ж1{MB}: 60, 60.0 %;
    группа Ж2{MC}: 10, unverified, процента нет (раньше было бы 10.0 %).

Самостоятельный скрипт (не pytest):
    python tests/test_step58_a07_unverified_overview.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from wb_energy_meter.group_repo_v2 import GroupRepoV2  # noqa: E402

from test_step28_reports_query import Rig, make_client, approx  # noqa: E402
from test_step49_acceptance_gaps import make_node, make_edge, publish  # noqa: E402

HOUR = 3600


def overview(client, f, t):
    r = client.post("/api/v2/overview/summary",
                    json={"from": f, "to": t, "timezone": "UTC"})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def report(client, dimension, ids, f, t, **extra):
    body = {"dimension": dimension, "scope_ids": ids, "from": f, "to": t,
            "timezone": "UTC"}
    body.update(extra)
    r = client.post("/api/v2/reports/query", json=body)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_no_topology_no_confirmed_total_no_percentages():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p1 = rig.make_point_at("P1", {0: 5.0, 1: 4.0})
        p2 = rig.make_point_at("P2", {0: 7.0, 1: 3.0})
        groups = GroupRepoV2(db)
        g = groups.add("Состав без сети")
        groups.add_member(g.id, p1.id, valid_from=0)
        groups.add_member(g.id, p2.id, valid_from=0)

        ov = overview(client, 0, HOUR)
        assert ov["object_total"] is None, ov["object_total"]       # итога объекта нет
        assert ov["object_input_point_ids"] == []
        (br,) = ov["branches"]
        assert br["mode"] == "sum" and approx(br["result"]["value"], 12.0), br
        assert br["result"]["structure_quality"] == "unverified", br["result"]
        assert br["percentage_of_object"] is None, br               # распределения нет
        assert br["percentage_of_object_reason"] in ("no_data", "structure_unverified")

        rep = report(client, "group", [g.id], 0, HOUR)
        row = rep["rows"][0]
        assert row["structure_confirmed"] is False, row
        assert row["result"]["structure_quality"] == "unverified"
        assert approx(row["result"]["value"], 12.0)

        # сравнение периодов — это изменение, а не распределение по объекту: A07 его
        # не скрывает (разность 7 − 12 = −5, −41,67 %), но строка помечена неподтверждённой
        cmp_rep = report(client, "group", [g.id], 1 * HOUR, 2 * HOUR,
                         compare={"from": 0, "to": HOUR})
        crow = cmp_rep["rows"][0]
        assert approx(crow["delta_value"], -5.0), crow
        assert approx(crow["delta_percentage"], round(-5.0 / 12.0 * 100, 2), tol=0.01), crow
        assert crow["structure_confirmed"] is False, crow
        print("[OK] A07-1: сети нет — итога объекта нет, сумма группы 12 помечена unverified без "
              "доли, строка отчёта structure_confirmed=false, процент изменения между периодами не скрыт")
    finally:
        db.close(); os.unlink(path)


def test_network_exists_group_outside_it_gets_no_percentage():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        ma = rig.make_point_at("MA", {0: 100.0})
        mb = rig.make_point_at("MB", {0: 60.0})
        mc = rig.make_point_at("MC", {0: 10.0})        # в сети нет
        src = make_node(client, "SRC", "Ввод", "source")
        pnl = make_node(client, "PNL", "Щит", "panel")
        load = make_node(client, "LD", "Нагрузка", "load")
        publish(client, [make_edge(client, src, pnl, ma), make_edge(client, pnl, load, mb)])

        groups = GroupRepoV2(db)
        g1 = groups.add("Ж1 в сети")
        g2 = groups.add("Ж2 вне сети")
        groups.add_member(g1.id, mb.id, valid_from=0)
        groups.add_member(g2.id, mc.id, valid_from=0)

        ov = overview(client, 0, HOUR)
        assert approx(ov["object_total"]["value"], 100.0), ov["object_total"]
        assert ov["object_total"]["structure_quality"] == "verified"
        by_name = {b["name"]: b for b in ov["branches"]}
        a, b = by_name["Ж1 в сети"], by_name["Ж2 вне сети"]
        assert approx(a["percentage_of_object"], 60.0), a
        assert a["result"]["structure_quality"] == "verified"
        assert approx(b["result"]["value"], 10.0), b
        assert b["result"]["structure_quality"] == "unverified", b["result"]
        assert b["percentage_of_object"] is None, b                  # было бы 10.0 %
        assert b["percentage_of_object_reason"] == "structure_unverified", b
        # сетевая ветвь уровня 1 (измерена и в сети) — по-прежнему с долей
        (nb,) = ov["network_branches"]
        assert approx(nb["percentage_of_object"], 60.0), nb

        rep = report(client, "group", [g1.id, g2.id], 0, HOUR)
        rows = {r["name"]: r for r in rep["rows"]}
        assert rows["Ж1 в сети"]["structure_confirmed"] is True
        assert rows["Ж2 вне сети"]["structure_confirmed"] is False
        print("[OK] A07-2: сеть есть — итог 100 (verified), Ж1 60.0 %, Ж2 (вне сети) 10 без процента "
              "(structure_unverified), сетевая ветвь 60.0 % сохранена")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_no_topology_no_confirmed_total_no_percentages()
    test_network_exists_group_outside_it_gets_no_percentage()
    print("\nA07 (Шаг 58) — все проверки пройдены.")

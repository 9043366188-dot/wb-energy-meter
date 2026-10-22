"""Тесты Шага 77 (партия 15, этап 15.6, A20): два одновременных запроса меняют питание одного узла —
один проходит, второй получает 409, двойного родителя нет.

Матрица приёмки (ТЗ §13, A20): «Два одновременных запроса создают основную установку / меняют
питание → один успешен, второй 409; не появляются пересекающиеся установки и двойной родитель».
Часть про основную установку закрыта `test_step15_binding_service.py::test_a20_...`; здесь — вторая
половина, но уже на уровне HTTP, как её вызывает интерфейс: два потока, два клиента, один узел.

Ожидания посчитаны вручную до запуска:

  Схема: источник S → щиты PA и PB; свободный щит X (без питания).
  1. «Соединить линией» (`POST /topology/edges/connect`) PA→X и PB→X одновременно, оба запроса несут
     одну и ту же ревизию R: ровно один 201, второй 409 `revision_conflict`; у X ровно одна
     действующая входящая линия (от победителя), строк `published` у X — одна.
  2. То же, но каждый поток сам читает ревизию прямо перед запросом (гонка за ревизию): ровно один
     201, второй — 409 (`revision_conflict`, если успел прочитать старую, либо `topology_conflict`
     «уже получает питание», если прочитал новую). Двойного родителя в обоих случаях нет.
  3. Публикация (`POST /topology/publish`) двух черновиков PA→X и PB→X одновременно с одной и той
     же `expected_configuration_revision`: ровно один 200, второй 409 `revision_conflict`;
     проигравший остаётся черновиком; действующая входящая линия у X одна. Затем проигравший
     повторяет с новой ревизией — это уже СМЕНА питания: PB→X действует, линия PA→X закрыта
     (`valid_to` проставлен, строка в истории), действующая по-прежнему одна.

  4. Основная установка (`POST /points/<id>/bindings`): два потока открывают основную привязку двум РАЗНЫМ
     точкам на один и тот же физический источник и профиль. Ровно один 201, второй 409
     (`revision_conflict`, если читал старую ревизию, либо `double_counting`, если читал новую); в базе
     у источника одна открытая привязка — у победителя, у проигравшей точки привязок нет. Раньше эта половина
     проверялась только на уровне репозитория (`test_step15_binding_service.py::test_a20_...`, без HTTP-кода
     и без чтения базы после гонки).

Каждый сценарий — 12 прогонов с барьером перед запросами, чтобы потоки стартовали вместе.

Самостоятельный скрипт: python tests/test_step77_topology_power_race.py
"""

from __future__ import annotations

import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, HERE)

from test_step49_acceptance_gaps import make_client, make_node, make_edge, publish, current_rev  # noqa: E402
from wb_energy_meter.repo import GroupRepo, MeterRepo  # noqa: E402
from wb_energy_meter.point_repo import MeterSourceRepo  # noqa: E402

ROUNDS = 12


def _race(app, requests_):
    """Запускает запросы одновременно (барьер), возвращает [(status, json)] в порядке `requests_`."""
    barrier = threading.Barrier(len(requests_))
    out = [None] * len(requests_)

    def worker(i, fn):
        c = app.test_client()
        barrier.wait()
        r = fn(c)
        out[i] = (r.status_code, r.get_json())

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(requests_)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def _edges_into(db, node_id):
    with db.read() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, from_node_id, state, valid_from, valid_to FROM electrical_edges "
            "WHERE to_node_id = ? ORDER BY id", (node_id,)).fetchall()]


def _active(rows):
    return [r for r in rows if r["state"] == "published" and r["valid_to"] is None]


def _setup(client):
    s = make_node(client, "S", "Ввод", "source")
    pa = make_node(client, "PA", "Щит A", "panel")
    pb = make_node(client, "PB", "Щит B", "panel")
    publish(client, [make_edge(client, s, pa), make_edge(client, s, pb)])
    return s, pa, pb


def test_connect_same_revision_one_wins():
    client, db, path, _reg = make_client()
    try:
        app = client.application
        _s, pa, pb = _setup(client)
        for i in range(ROUNDS):
            x = make_node(client, "X%d" % i, "Щит X%d" % i, "panel")
            rev = current_rev(client)

            def conn(parent):
                return lambda c: c.post("/api/v2/topology/edges/connect", json={
                    "from_node_id": parent["id"], "to_node_id": x["id"], "expected_revision": rev})

            res = _race(app, [conn(pa), conn(pb)])
            statuses = sorted(r[0] for r in res)
            assert statuses == [201, 409], (i, res)
            loser = next(r[1] for r in res if r[0] == 409)
            assert loser["code"] == "revision_conflict", (i, loser)
            rows = _edges_into(db, x["id"])
            assert len(_active(rows)) == 1 and len(rows) == 1, (i, rows)
            winner_parent = next(p for p, r in zip((pa, pb), res) if r[0] == 201)
            assert rows[0]["from_node_id"] == winner_parent["id"], (i, rows, res)
        print("[OK] A20: «Соединить линией» двумя потоками с одной ревизией — %d прогонов, всегда один 201 и один "
              "409 revision_conflict, у узла одна действующая линия от победителя" % ROUNDS)
    finally:
        db.close()
        os.unlink(path)


def test_connect_each_reads_revision_one_wins():
    client, db, path, _reg = make_client()
    try:
        app = client.application
        _s, pa, pb = _setup(client)
        seen_codes = set()
        for i in range(ROUNDS):
            x = make_node(client, "Y%d" % i, "Щит Y%d" % i, "panel")

            def conn(parent):
                def fn(c):
                    rev = c.get("/api/v2/revision").get_json()["configuration_revision"]
                    return c.post("/api/v2/topology/edges/connect", json={
                        "from_node_id": parent["id"], "to_node_id": x["id"], "expected_revision": rev})
                return fn

            res = _race(app, [conn(pa), conn(pb)])
            statuses = sorted(r[0] for r in res)
            assert statuses == [201, 409], (i, res)
            loser = next(r[1] for r in res if r[0] == 409)
            assert loser["code"] in ("revision_conflict", "topology_conflict"), (i, loser)
            seen_codes.add(loser["code"])
            rows = _edges_into(db, x["id"])
            assert len(_active(rows)) == 1 and len(rows) == 1, (i, rows)
        print("[OK] A20: тот же сценарий, ревизию каждый поток читает сам — %d прогонов, всегда один 201 и один 409 "
              "(коды проигравшего: %s), двойного родителя нет" % (ROUNDS, sorted(seen_codes)))
    finally:
        db.close()
        os.unlink(path)


def test_publish_same_revision_one_wins_then_switch_closes_old_line():
    client, db, path, _reg = make_client()
    try:
        app = client.application
        _s, pa, pb = _setup(client)
        for i in range(ROUNDS):
            x = make_node(client, "Z%d" % i, "Щит Z%d" % i, "panel")
            da = make_edge(client, pa, x)
            db_ = make_edge(client, pb, x)
            rev = current_rev(client)

            def pub(draft):
                return lambda c: c.post("/api/v2/topology/publish", json={
                    "edge_ids": [draft["id"]], "expected_configuration_revision": rev})

            res = _race(app, [pub(da), pub(db_)])
            statuses = sorted(r[0] for r in res)
            assert statuses == [200, 409], (i, res)
            loser_idx = next(k for k, r in enumerate(res) if r[0] == 409)
            assert res[loser_idx][1]["code"] == "revision_conflict", (i, res[loser_idx])
            rows = _edges_into(db, x["id"])
            assert len(_active(rows)) == 1, (i, rows)
            drafts = [r for r in rows if r["state"] == "draft"]
            loser_draft = (da, db_)[loser_idx]
            assert [r["id"] for r in drafts] == [loser_draft["id"]], (i, rows, loser_draft)

            # проигравший повторяет со свежей ревизией: это смена питания, а не второй родитель
            r = client.post("/api/v2/topology/publish", json={
                "edge_ids": [loser_draft["id"]], "expected_configuration_revision": current_rev(client)})
            assert r.status_code == 200, (i, r.get_json())
            rows = _edges_into(db, x["id"])
            active = _active(rows)
            assert len(active) == 1 and active[0]["id"] == loser_draft["id"], (i, rows)
            closed = [r_ for r_ in rows if r_["state"] == "published" and r_["valid_to"] is not None]
            assert len(closed) == 1, (i, rows)
        print("[OK] A20: публикация двумя потоками с одной ревизией — %d прогонов: один 200, второй 409 "
              "revision_conflict и остаётся черновиком; повтор со свежей ревизией меняет питание — действующая "
              "линия одна, прежняя закрыта valid_to" % ROUNDS)
    finally:
        db.close()
        os.unlink(path)


def _open_bindings(db, source_id):
    with db.read() as c:
        return [dict(r) for r in c.execute(
            "SELECT point_id, meter_source_id, channel_profile, role FROM point_bindings "
            "WHERE meter_source_id = ? AND valid_to IS NULL ORDER BY id", (source_id,)).fetchall()]


def _all_bindings_of_point(db, point_id):
    with db.read() as c:
        return [dict(r) for r in c.execute(
            "SELECT id FROM point_bindings WHERE point_id = ?", (point_id,)).fetchall()]


def test_open_primary_binding_same_source_one_wins_no_overlap():
    client, db, path, _reg = make_client()
    try:
        app = client.application
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        seen_codes = set()
        for i in range(ROUNDS):
            m = meters.add("dev-77.%d" % i, "Прибор гонки %d" % i)
            src = sources.open_source(m.id, "wb8-main", "dev-77.%d" % i)
            points = []
            for k in (1, 2):
                r = client.post("/api/v2/points", json={"code": "p77.%d.%d" % (i, k), "name": "Точка %d.%d" % (i, k)})
                assert r.status_code == 201, r.get_json()
                points.append(r.get_json())
            fixed_rev = current_rev(client) if i % 2 == 0 else None  # чётные прогоны — одна ревизия на двоих

            def bind(point):
                def fn(c):
                    rev = fixed_rev if fixed_rev is not None else c.get(
                        "/api/v2/revision").get_json()["configuration_revision"]
                    return c.post("/api/v2/points/%d/bindings" % point["id"], json={
                        "meter_source_id": src.id, "channel_profile": "total_3p", "expected_revision": rev})
                return fn

            res = _race(app, [bind(points[0]), bind(points[1])])
            statuses = sorted(r_[0] for r_ in res)
            assert statuses == [201, 409], (i, res)
            loser_idx = next(k for k, r_ in enumerate(res) if r_[0] == 409)
            winner_idx = 1 - loser_idx
            code = res[loser_idx][1]["code"]
            assert code in ("revision_conflict", "double_counting"), (i, res[loser_idx])
            if fixed_rev is not None:
                assert code == "revision_conflict", (i, res[loser_idx])
            seen_codes.add(code)
            opened = _open_bindings(db, src.id)
            assert len(opened) == 1, (i, opened)
            assert opened[0]["point_id"] == points[winner_idx]["id"], (i, opened, res)
            assert opened[0]["channel_profile"] == "total_3p" and opened[0]["role"] == "primary", (i, opened)
            assert _all_bindings_of_point(db, points[loser_idx]["id"]) == [], (i, points[loser_idx])
        print("[OK] A20: основная установка двумя потоками на один источник и профиль — %d прогонов, всегда один 201 "
              "и один 409 (коды проигравшего: %s); в базе одна открытая привязка — у победителя, у проигравшей "
              "точки привязок нет" % (ROUNDS, sorted(seen_codes)))
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    test_connect_same_revision_one_wins()
    test_connect_each_reads_revision_one_wins()
    test_publish_same_revision_one_wins_then_switch_closes_old_line()
    test_open_primary_binding_same_source_one_wins_no_overlap()
    print("\nВсе тесты Шага 77 (A20, гонки: смена питания и основная установка) пройдены.")

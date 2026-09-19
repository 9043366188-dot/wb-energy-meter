"""Шаг 36 (партия 6, задача 1): простой режим — «ввод»/«питается от»
как интерфейсный слой поверх узлов/связей (docs/TZ-batch6-simple-mode.md
§2/§9), без изменения модели: НИКАКОГО parent_meter_id, дерево остаётся
только в electrical_edges (см. wb_energy_meter/simple_mode_service.py).

ОБЯЗАТЕЛЬНЫЙ сквозной сценарий (§9): три точки на чистой БД, у одной
«ввод», две другие «питаются от» первой, расход 100/70/40 — «Обзор»
даёт итог 100 (через ввод, не 210 суммой), небаланс −10 и −10% — и
внутри реально построено дерево (узлы+опубликованные связи), не просто
совпали числа.

Каждый шаг — только через HTTP /api/v2 (тот же принцип, что и в
test_step34_e2e_ui_creation_path.py), кроме заполнения агрегата энергии
(seed_energy) — у /api/v2 нет и не должно быть ручки "вписать
историческое показание".

К каждой проверке "отказ" — парная "легитимный запрос работает" (см.
AGENTS.md: "тест «доступ запрещён» ничего не доказывает сам по себе").

Самостоятельный скрипт (не pytest):
    python tests/test_step36_simple_mode.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate

HOUR = 3600


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


def seed_energy(db, device_id, kwh):
    meters = MeterRepo(db, GroupRepo(db))
    m = meters.get_by_device_id(device_id)
    assert m is not None, f"meter {device_id} должен уже существовать"
    AggregateRepo(db).upsert(HourlyAggregate(
        meter_id=m.id, period_start=0, period_end=HOUR,
        ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
        p_avg=None, p_max=None, samples_count=1, quality_flag="ok",
        computed_at=0))


def _make_point(client, code, name):
    rev = current_rev(client)
    r = client.post("/api/v2/points", json={
        "code": code, "name": name, "expected_revision": rev})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _bind_meter(client, point_id, device_id, display_name):
    rev = current_rev(client)
    r = client.post(f"/api/v2/points/{point_id}/bindings", json={
        "new_meter": {"controller_key": "wb8-main", "device_id": device_id,
                      "display_name": display_name},
        "channel_profile": "total_3p", "valid_from": 0, "expected_revision": rev,
    })
    assert r.status_code == 201, r.get_json()


def _set_power_supply(client, point_id, *, is_input=None, fed_from_point_id="__unset__"):
    rev = current_rev(client)
    body = {"expected_revision": rev}
    if is_input is not None:
        body["is_input"] = is_input
    if fed_from_point_id != "__unset__":
        body["fed_from_point_id"] = fed_from_point_id
    return client.patch(f"/api/v2/points/{point_id}", json=body)


def _published_edges(client):
    r = client.get("/api/v2/topology/edges?state=published")
    assert r.status_code == 200
    return r.get_json()


def _nodes(client, include_archived=False):
    url = "/api/v2/topology/nodes"
    if include_archived:
        url += "?include_archived=1"
    r = client.get(url)
    assert r.status_code == 200
    return r.get_json()


# ---------------------------------------------------------------------
# Сквозной тест (§9, ОБЯЗАТЕЛЬНЫЙ)
# ---------------------------------------------------------------------

def test_e2e_three_points_input_and_two_consumers_overview_via_input():
    client, db, path = make_client()
    try:
        p_input = _make_point(client, "vvod", "Ввод объекта")
        p_c1 = _make_point(client, "nagr-1", "ЩР-1")
        p_c2 = _make_point(client, "nagr-2", "ЩР-2")

        _bind_meter(client, p_input["id"], "dev-vvod", "Счётчик ввода")
        _bind_meter(client, p_c1["id"], "dev-nagr-1", "Счётчик ЩР-1")
        _bind_meter(client, p_c2["id"], "dev-nagr-2", "Счётчик ЩР-2")

        # Простой режим: галочка "ввод" у первой точки ------------------
        r = _set_power_supply(client, p_input["id"], is_input=True)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["power_supply"] == {
            "is_input": True, "fed_from_point_id": None}, r.get_json()

        # "Питается от" у двух других, с другого конца — как в реальном
        # интерфейсе, каждая точка сама указывает свой источник ---------
        r = _set_power_supply(client, p_c1["id"], fed_from_point_id=p_input["id"])
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["power_supply"] == {
            "is_input": False, "fed_from_point_id": p_input["id"]}, r.get_json()

        r = _set_power_supply(client, p_c2["id"], fed_from_point_id=p_input["id"])
        assert r.status_code == 200, r.get_json()

        # Внутри должно быть построено дерево, а не просто "числа сошлись"
        # (§9: "проверить, что дерево построено, а не что «числа сошлись
        # случайно»") — 3 узла точек (panel) + 1 узел-источник (source),
        # 3 опубликованные связи. --------------------------------------
        nodes = _nodes(client)
        assert len(nodes) == 4, nodes
        source_nodes = [n for n in nodes if n["kind"] == "source"]
        panel_nodes = [n for n in nodes if n["kind"] == "panel"]
        assert len(source_nodes) == 1, nodes
        assert len(panel_nodes) == 3, nodes

        edges = _published_edges(client)
        assert len(edges) == 3, edges
        by_point = {e["primary_point_id"]: e for e in edges}
        assert set(by_point.keys()) == {p_input["id"], p_c1["id"], p_c2["id"]}
        input_edge = by_point[p_input["id"]]
        input_node_id = input_edge["to_node_id"]
        assert input_edge["from_node_id"] == source_nodes[0]["id"]
        assert by_point[p_c1["id"]]["from_node_id"] == input_node_id
        assert by_point[p_c2["id"]]["from_node_id"] == input_node_id

        # Группа-ветвь с двумя потребителями (обычная принадлежность,
        # никак не связанная с топологией) — нужна, чтобы у "Обзора" был
        # знаменатель для небаланса (§8.2: небаланс = итог объекта минус
        # сумма ВЕРХНЕУРОВНЕВЫХ ветвей/групп). ---------------------------
        rev = current_rev(client)
        r = client.post("/api/v2/groups", json={
            "name": "Нагрузка цеха", "expected_revision": rev})
        assert r.status_code == 201, r.get_json()
        group = r.get_json()
        for pid in (p_c1["id"], p_c2["id"]):
            rev = current_rev(client)
            r = client.post(f"/api/v2/groups/{group['id']}/members", json={
                "point_id": pid, "expected_revision": rev})
            assert r.status_code == 201, r.get_json()

        # Расход за период: 100 / 70 / 40 (единственный не-HTTP шаг) -----
        seed_energy(db, "dev-vvod", 100.0)
        seed_energy(db, "dev-nagr-1", 70.0)
        seed_energy(db, "dev-nagr-2", 40.0)

        r = client.post("/api/v2/overview/summary",
                         json={"from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["object_input_point_ids"] == [p_input["id"]], body
        assert body["object_total"]["value"] == 100.0, (
            "итог должен браться через назначенный ввод (100), а не суммой "
            "всех приборов (100+70+40=210)", body)
        assert body["imbalance_value"] == -10.0, body
        assert body["imbalance_percent"] == -10.0, body

        # Точка ввода не считается "без группы" (см. test_step30) --------
        r = client.get("/api/v2/validation")
        no_group_ids = {it["point_id"] for it in r.get_json()["points_without_group"]}
        assert p_input["id"] not in no_group_ids

        print("[OK] сквозной сценарий партии 6: простой режим (ввод + 2×"
              "«питается от») на чистой БД — «Обзор» 100 через ввод, "
              "небаланс −10/−10%, дерево реально построено (4 узла, 3 "
              "опубликованные связи)")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# Циклическое питание — отказ понятным текстом + парная легитимная проверка
# ---------------------------------------------------------------------

def test_cyclic_feed_rejected_in_point_terms_and_legit_request_still_works():
    client, db, path = make_client()
    try:
        p_a = _make_point(client, "a", "Точка А")
        p_b = _make_point(client, "b", "Точка Б")

        # Б питается от А — легитимно, проходит.
        r = _set_power_supply(client, p_b["id"], fed_from_point_id=p_a["id"])
        assert r.status_code == 200, r.get_json()
        edges_before = _published_edges(client)
        assert len(edges_before) == 1

        # Теперь А "питается от" Б — замыкает цикл А->Б->А, должно быть
        # отклонено понятным текстом В ТЕРМИНАХ ТОЧЕК (не "node 3"/"edge 7").
        r = _set_power_supply(client, p_a["id"], fed_from_point_id=p_b["id"])
        assert r.status_code == 409, r.get_json()
        d = r.get_json()
        assert d["code"] == "simple_mode_conflict", d
        msg = d["message"]
        assert "узел" not in msg.lower() and "edge" not in msg.lower(), (
            "сообщение о цикле должно быть в терминах точек учёта, а не "
            "узлов/связей", msg)
        assert "точка а" in msg.lower() or "точка б" in msg.lower(), (
            "сообщение должно называть точки по имени", msg)

        # Ничего не сохранено: как было одна связь (Б от А), так и осталась.
        edges_after = _published_edges(client)
        assert len(edges_after) == 1, (
            "циклическая попытка не должна ничего сохранять", edges_after)
        r = client.get(f"/api/v2/points/{p_a['id']}")
        assert r.get_json()["power_supply"] == {
            "is_input": False, "fed_from_point_id": None}, (
            "точка А не должна была измениться после отклонённой попытки")

        # Парная легитимная проверка: А как раз может стать вводом
        # (независимое, несвязанное с циклом действие) — убеждаемся, что
        # отклонённая попытка не оставила состояние в поломанном виде.
        r = _set_power_supply(client, p_a["id"], is_input=True)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["power_supply"]["is_input"] is True

        print("[OK] циклическое питание отклонено текстом в терминах точек, "
              "ничего не сохранено; легитимный запрос (А — ввод) отработал")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# Ввод не может одновременно "питаться от" — отказ + легитимная проверка
# ---------------------------------------------------------------------

def test_input_cannot_be_fed_from_and_legit_input_alone_works():
    client, db, path = make_client()
    try:
        p_a = _make_point(client, "a", "Ввод")
        p_b = _make_point(client, "b", "Точка Б")

        rev = current_rev(client)
        r = client.patch(f"/api/v2/points/{p_a['id']}", json={
            "is_input": True, "fed_from_point_id": p_b["id"],
            "expected_revision": rev})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "simple_mode_conflict"
        assert not _published_edges(client)

        r = _set_power_supply(client, p_a["id"], is_input=True)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["power_supply"] == {
            "is_input": True, "fed_from_point_id": None}

        print("[OK] «ввод» + «питается от» одновременно отклонены; "
              "легитимный запрос (только «ввод») работает")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# Точка не может питаться сама от себя — отказ + легитимная проверка
# ---------------------------------------------------------------------

def test_self_feed_rejected_and_legit_feed_from_other_point_works():
    client, db, path = make_client()
    try:
        p_a = _make_point(client, "a", "Точка А")
        p_b = _make_point(client, "b", "Точка Б")

        r = _set_power_supply(client, p_a["id"], fed_from_point_id=p_a["id"])
        assert r.status_code == 409, r.get_json()
        assert not _published_edges(client)

        r = _set_power_supply(client, p_a["id"], fed_from_point_id=p_b["id"])
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["power_supply"]["fed_from_point_id"] == p_b["id"]

        print("[OK] самопитание отклонено; легитимное «питается от другой "
              "точки» работает")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# Смена источника (редактирование) — старая связь вытесняется без конфликта
# ---------------------------------------------------------------------

def test_change_fed_from_supersedes_old_edge_without_conflict():
    client, db, path = make_client()
    try:
        p_a = _make_point(client, "a", "Источник А")
        p_b = _make_point(client, "b", "Источник Б")
        p_c = _make_point(client, "c", "Точка В")

        r = _set_power_supply(client, p_c["id"], fed_from_point_id=p_a["id"])
        assert r.status_code == 200, r.get_json()
        edges1 = _published_edges(client)
        assert len(edges1) == 1 and edges1[0]["primary_point_id"] == p_c["id"]
        old_edge_id = edges1[0]["id"]

        # Редактирование — переключить В на питание от Б, это НЕ конфликт
        # (§4 «Структура»: редактирование должно быть доступно).
        r = _set_power_supply(client, p_c["id"], fed_from_point_id=p_b["id"])
        assert r.status_code == 200, r.get_json()
        edges2 = _published_edges(client)
        assert len(edges2) == 1, edges2
        assert edges2[0]["primary_point_id"] == p_c["id"]
        assert edges2[0]["id"] != old_edge_id, "должна появиться новая связь"

        r = client.get(f"/api/v2/topology/edges/{old_edge_id}")
        assert r.get_json()["state"] == "published"
        assert r.get_json()["valid_to"] is not None, "старая связь должна быть закрыта"

        print("[OK] смена «питается от» вытесняет старую связь без конфликта")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# Снятие питания — закрывает связь, best-effort архивирует источник-ввод
# ---------------------------------------------------------------------

def test_clear_power_supply_retires_edge_and_archives_orphan_source():
    client, db, path = make_client()
    try:
        p_a = _make_point(client, "a", "Ввод")
        r = _set_power_supply(client, p_a["id"], is_input=True)
        assert r.status_code == 200, r.get_json()
        assert len(_published_edges(client)) == 1

        nodes_before = _nodes(client)
        source_id = next(n["id"] for n in nodes_before if n["kind"] == "source")

        r = _set_power_supply(client, p_a["id"], is_input=False, fed_from_point_id=None)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["power_supply"] == {
            "is_input": False, "fed_from_point_id": None}
        assert not _published_edges(client)

        nodes_after = _nodes(client, include_archived=True)
        source_after = next(n for n in nodes_after if n["id"] == source_id)
        assert source_after["archived_at"] is not None, (
            "осиротевший служебный узел-источник должен быть подчищен")

        # Легитимная проверка: точку снова можно сделать вводом (не
        # заблокирована архивным узлом-источником с тем же кодом).
        r = _set_power_supply(client, p_a["id"], is_input=True)
        assert r.status_code == 200, r.get_json()

        print("[OK] снятие «ввода» закрывает связь и подчищает узел-источник; "
              "повторное включение работает")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# "Добавить потребителей" из карточки ввода — та же связь с другого конца
# ---------------------------------------------------------------------

def test_add_consumers_reverse_action_from_input_card():
    client, db, path = make_client()
    try:
        p_in = _make_point(client, "vvod", "Ввод")
        p_c1 = _make_point(client, "c1", "Потребитель 1")
        p_c2 = _make_point(client, "c2", "Потребитель 2")

        r = _set_power_supply(client, p_in["id"], is_input=True)
        assert r.status_code == 200, r.get_json()

        # "Добавить потребителей" — тот же вызов с другого конца, по
        # очереди для каждой выбранной точки (см. docs/TZ-batch6 §2:
        # "это та же самая связь, введённая с другого конца").
        for consumer in (p_c1, p_c2):
            r = _set_power_supply(client, consumer["id"], fed_from_point_id=p_in["id"])
            assert r.status_code == 200, r.get_json()

        edges = _published_edges(client)
        assert len(edges) == 3
        by_point = {e["primary_point_id"]: e for e in edges}
        input_node_id = by_point[p_in["id"]]["to_node_id"]
        assert by_point[p_c1["id"]]["from_node_id"] == input_node_id
        assert by_point[p_c2["id"]]["from_node_id"] == input_node_id

        print("[OK] «добавить потребителей» из карточки ввода создаёт ту же связь")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_e2e_three_points_input_and_two_consumers_overview_via_input()
    test_cyclic_feed_rejected_in_point_terms_and_legit_request_still_works()
    test_input_cannot_be_fed_from_and_legit_input_alone_works()
    test_self_feed_rejected_and_legit_feed_from_other_point_works()
    test_change_fed_from_supersedes_old_edge_without_conflict()
    test_clear_power_supply_retires_edge_and_archives_orphan_source()
    test_add_consumers_reverse_action_from_input_card()
    print("\nВсе тесты простого режима (Шаг 36) пройдены.")

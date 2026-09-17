"""Тесты Шага 25 (этап E/F): HTTP-слой /api/v2/groups* (ТЗ §4.4/§9.2).

Самостоятельный скрипт (не pytest):
    python tests/test_step25_api_v2_groups.py

Проверяет, что api_v2.py правильно транслирует исключения GroupRepoV2
(циклы/область видимости категории/конфликт членства/not_found) в HTTP
409/404/400 с envelope {"code","message",...}, и что маршруты верно
прокидывают параметры (parent_id-фильтр, at, include_closed) — это самая
ответственная часть слоя, написана лично, как и test_step18_api_v2.py."""

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


def add_point(client, code, name):
    r = client.post("/api/v2/points", json={"code": code, "name": name})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def test_groups_crud_and_list_filters():
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/groups", json={"name": "Объект", "category": "production"})
        assert r.status_code == 201, r.get_json()
        root = r.get_json()
        assert root["parent_id"] is None
        assert root["color"]

        r = client.post("/api/v2/groups", json={
            "name": "Цех", "category": "production", "parent_id": root["id"]})
        assert r.status_code == 201, r.get_json()
        child = r.get_json()
        assert child["parent_id"] == root["id"]

        r = client.get(f"/api/v2/groups/{child['id']}")
        assert r.status_code == 200 and r.get_json()["name"] == "Цех"

        r = client.get("/api/v2/groups")
        names = {g["name"] for g in r.get_json()}
        assert names == {"Объект", "Цех"}

        r = client.get("/api/v2/groups?parent_id=null")
        assert [g["name"] for g in r.get_json()] == ["Объект"]

        r = client.get(f"/api/v2/groups?parent_id={root['id']}")
        assert [g["name"] for g in r.get_json()] == ["Цех"]
        print("[OK] test_groups_crud_and_list_filters")
    finally:
        db.close(); os.unlink(path)


def test_group_not_found_404():
    client, db, path = make_client()
    try:
        r = client.get("/api/v2/groups/99999")
        assert r.status_code == 404
        assert r.get_json()["code"] == "not_found"
        print("[OK] test_group_not_found_404")
    finally:
        db.close(); os.unlink(path)


def test_group_duplicate_name_bad_request():
    client, db, path = make_client()
    try:
        client.post("/api/v2/groups", json={"name": "Склад"})
        r = client.post("/api/v2/groups", json={"name": "склад"})
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "bad_request"
        print("[OK] test_group_duplicate_name_bad_request")
    finally:
        db.close(); os.unlink(path)


def test_group_category_conflict_409():
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/groups", json={"name": "Арендаторы", "category": "tenant"})
        tenants_id = r.get_json()["id"]
        r = client.post("/api/v2/groups", json={
            "name": "Цех", "category": "production", "parent_id": tenants_id})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "category_conflict"
        print("[OK] test_group_category_conflict_409")
    finally:
        db.close(); os.unlink(path)


def test_group_set_parent_cycle_409():
    client, db, path = make_client()
    try:
        a = client.post("/api/v2/groups", json={"name": "A"}).get_json()["id"]
        b = client.post("/api/v2/groups", json={"name": "B", "parent_id": a}).get_json()["id"]

        r = client.patch(f"/api/v2/groups/{a}", json={"parent_id": b})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "cycle_conflict"

        # исходная иерархия не должна была измениться
        r = client.get(f"/api/v2/groups/{a}")
        assert r.get_json()["parent_id"] is None
        print("[OK] test_group_set_parent_cycle_409")
    finally:
        db.close(); os.unlink(path)


def test_group_set_parent_success():
    client, db, path = make_client()
    try:
        a = client.post("/api/v2/groups", json={"name": "A"}).get_json()["id"]
        b = client.post("/api/v2/groups", json={"name": "B"}).get_json()["id"]
        c = client.post("/api/v2/groups", json={"name": "C", "parent_id": a}).get_json()["id"]

        r = client.patch(f"/api/v2/groups/{c}", json={"parent_id": b})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["parent_id"] == b
        print("[OK] test_group_set_parent_success")
    finally:
        db.close(); os.unlink(path)


def test_group_members_crud():
    client, db, path = make_client()
    try:
        g = client.post("/api/v2/groups", json={"name": "Группа"}).get_json()["id"]
        p1 = add_point(client, "p1", "Точка 1")
        p2 = add_point(client, "p2", "Точка 2")

        # valid_from/at заданы явно и разнесены во времени: close_binding-
        # подобная проверка ("момент закрытия должен быть позже начала",
        # см. GroupRepoV2.remove_member) требует at > valid_from -- при
        # открытии и закрытии в один и тот же реальный момент (та же
        # секунда) она обоснованно откажет, так же как и у
        # PointBindingRepo.close_binding.
        r = client.post(f"/api/v2/groups/{g}/members", json={"point_id": p1, "valid_from": 1000})
        assert r.status_code == 201, r.get_json()
        r = client.post(f"/api/v2/groups/{g}/members", json={"point_id": p2, "valid_from": 1000})
        assert r.status_code == 201, r.get_json()

        r = client.get(f"/api/v2/groups/{g}/members")
        assert {m["point_id"] for m in r.get_json()} == {p1, p2}

        r = client.get(f"/api/v2/points/{p1}/groups")
        assert [m["group_id"] for m in r.get_json()] == [g]

        r = client.delete(f"/api/v2/groups/{g}/members/{p1}?at=2000")
        assert r.status_code == 204, r.get_json()

        r = client.get(f"/api/v2/groups/{g}/members")
        assert {m["point_id"] for m in r.get_json()} == {p2}

        r = client.get(f"/api/v2/groups/{g}/members?include_closed=1")
        assert {m["point_id"] for m in r.get_json()} == {p1, p2}
        print("[OK] test_group_members_crud")
    finally:
        db.close(); os.unlink(path)


def test_group_member_duplicate_conflict_409():
    client, db, path = make_client()
    try:
        g = client.post("/api/v2/groups", json={"name": "Группа"}).get_json()["id"]
        p1 = add_point(client, "p1", "Точка 1")
        client.post(f"/api/v2/groups/{g}/members", json={"point_id": p1})

        r = client.post(f"/api/v2/groups/{g}/members", json={"point_id": p1})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "conflict"
        print("[OK] test_group_member_duplicate_conflict_409")
    finally:
        db.close(); os.unlink(path)


def test_group_member_missing_point_404():
    client, db, path = make_client()
    try:
        g = client.post("/api/v2/groups", json={"name": "Группа"}).get_json()["id"]
        r = client.post(f"/api/v2/groups/{g}/members", json={"point_id": 99999})
        assert r.status_code == 404, r.get_json()
        assert r.get_json()["code"] == "not_found"
        print("[OK] test_group_member_missing_point_404")
    finally:
        db.close(); os.unlink(path)


def test_group_remove_member_without_membership_404():
    client, db, path = make_client()
    try:
        g = client.post("/api/v2/groups", json={"name": "Группа"}).get_json()["id"]
        p1 = add_point(client, "p1", "Точка 1")
        r = client.delete(f"/api/v2/groups/{g}/members/{p1}")
        assert r.status_code == 404, r.get_json()
        print("[OK] test_group_remove_member_without_membership_404")
    finally:
        db.close(); os.unlink(path)


def test_group_effective_members_dedup_via_http():
    """Тот же сценарий дедупликации, что и в test_step24, но через HTTP —
    проверяем, что маршрут не теряет/не искажает via при сериализации."""
    client, db, path = make_client()
    try:
        root = client.post("/api/v2/groups", json={"name": "Объект"}).get_json()["id"]
        child = client.post("/api/v2/groups", json={"name": "Цех", "parent_id": root}).get_json()["id"]
        p1 = add_point(client, "p1", "Точка 1")
        p2 = add_point(client, "p2", "Точка 2")

        client.post(f"/api/v2/groups/{root}/members", json={"point_id": p1})
        client.post(f"/api/v2/groups/{child}/members", json={"point_id": p1})
        client.post(f"/api/v2/groups/{child}/members", json={"point_id": p2})

        r = client.get(f"/api/v2/groups/{root}/effective-members")
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        by_point = {it["point_id"]: it for it in body["points"]}
        assert set(by_point.keys()) == {p1, p2}
        assert sorted(by_point[p1]["via"]) == sorted([root, child])
        assert by_point[p1]["code"] == "p1"
        assert by_point[p2]["via"] == [child]
        print("[OK] test_group_effective_members_dedup_via_http")
    finally:
        db.close(); os.unlink(path)


def test_group_effective_members_bad_at_400():
    client, db, path = make_client()
    try:
        g = client.post("/api/v2/groups", json={"name": "Группа"}).get_json()["id"]
        r = client.get(f"/api/v2/groups/{g}/effective-members?at=not-a-number")
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "bad_request"
        print("[OK] test_group_effective_members_bad_at_400")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_groups_crud_and_list_filters()
    test_group_not_found_404()
    test_group_duplicate_name_bad_request()
    test_group_category_conflict_409()
    test_group_set_parent_cycle_409()
    test_group_set_parent_success()
    test_group_members_crud()
    test_group_member_duplicate_conflict_409()
    test_group_member_missing_point_404()
    test_group_remove_member_without_membership_404()
    test_group_effective_members_dedup_via_http()
    test_group_effective_members_bad_at_400()
    print("[ALL OK] test_step25_api_v2_groups")

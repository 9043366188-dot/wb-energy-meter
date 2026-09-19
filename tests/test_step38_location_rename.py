"""Шаг 38 (партия 6, задача 3): инлайн-переименование места учёта
(docs/TZ-batch6-simple-mode.md §4 — "инлайн-редактирование без ухода
с экрана"). Раньше PATCH /api/v2/locations/<id> принимал только
parent_id и archived — имя/код места можно было задать один раз, при
создании (LocationRepo.add), и никак не поменять потом.

Точки уже умели это (см. PATCH /api/v2/points/<id> —
MeteringPointRepo.update_fields для name/description/
installation_note существовал до этой партии) — здесь та же
возможность добавлена местам, отдельного нового маршрута не
понадобилось (LocationRepo.update_fields + существующий PATCH).

Каждая проверка "отказ" сопровождается парной "легитимный запрос
работает".

Самостоятельный скрипт (не pytest):
    python tests/test_step38_location_rename.py
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


def test_location_rename_and_recode():
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/locations", json={
            "name": "Щитовая старое имя", "kind": "room", "code": "old-code"})
        assert r.status_code == 201, r.get_json()
        loc = r.get_json()

        rev = current_rev(client)
        r = client.patch(f"/api/v2/locations/{loc['id']}", json={
            "name": "Щитовая новое имя", "code": "new-code",
            "expected_revision": rev})
        assert r.status_code == 200, r.get_json()
        updated = r.get_json()
        assert updated["name"] == "Щитовая новое имя"
        assert updated["code"] == "new-code"

        r = client.get(f"/api/v2/locations/{loc['id']}")
        assert r.get_json()["name"] == "Щитовая новое имя"
        assert r.get_json()["code"] == "new-code"

        # родитель/parent_id по-прежнему не тронут переименованием
        assert r.get_json()["parent_id"] is None
        print("[OK] переименование и смена кода места через PATCH сохраняются")
    finally:
        db.close()
        os.unlink(path)


def test_location_rename_empty_name_rejected_valid_still_works():
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/locations", json={"name": "Комната 1", "kind": "room"})
        loc = r.get_json()

        rev = current_rev(client)
        r = client.patch(f"/api/v2/locations/{loc['id']}", json={
            "name": "   ", "expected_revision": rev})
        assert r.status_code == 400, r.get_json()

        r = client.get(f"/api/v2/locations/{loc['id']}")
        assert r.get_json()["name"] == "Комната 1"  # ничего не сломалось отказом

        rev = current_rev(client)
        r = client.patch(f"/api/v2/locations/{loc['id']}", json={
            "name": "Комната 1А", "expected_revision": rev})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["name"] == "Комната 1А"
        print("[OK] пустое имя отклонено; легитимное переименование работает")
    finally:
        db.close()
        os.unlink(path)


def test_location_rename_duplicate_code_rejected_unique_still_works():
    client, db, path = make_client()
    try:
        client.post("/api/v2/locations", json={
            "name": "Комната А", "kind": "room", "code": "room-a"})
        r = client.post("/api/v2/locations", json={
            "name": "Комната Б", "kind": "room", "code": "room-b"})
        loc_b = r.get_json()

        rev = current_rev(client)
        r = client.patch(f"/api/v2/locations/{loc_b['id']}", json={
            "code": "room-a", "expected_revision": rev})
        assert r.status_code == 400, r.get_json()

        r = client.get(f"/api/v2/locations/{loc_b['id']}")
        assert r.get_json()["code"] == "room-b"  # не перезаписалось отказом

        rev = current_rev(client)
        r = client.patch(f"/api/v2/locations/{loc_b['id']}", json={
            "code": "room-b2", "expected_revision": rev})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["code"] == "room-b2"
        print("[OK] дублирующийся код места отклонён; уникальный код — легитимно применяется")
    finally:
        db.close()
        os.unlink(path)


def test_structure_points_exposes_what_is_missing():
    """§4 "показать чего не хватает" — точка без прибора/места/питания
    должна быть отличима по уже отдаваемым /api/v2/structure/points
    полям (bound, location_id, is_input, fed_from_point_id) без нового
    маршрута — фронтенд это только показывает."""
    client, db, path = make_client()
    try:
        rev = current_rev(client)
        r = client.post("/api/v2/points", json={
            "code": "bare", "name": "Точка без ничего", "expected_revision": rev})
        point = r.get_json()

        r = client.get("/api/v2/structure/points")
        item = next(i for i in r.get_json()["points"] if i["point_id"] == point["id"])
        assert item["bound"] is False
        assert item["location_id"] is None
        assert item["is_input"] is False
        assert item["fed_from_point_id"] is None
        print("[OK] /api/v2/structure/points уже отдаёт всё нужное для \"чего не хватает\"")
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    test_location_rename_and_recode()
    test_location_rename_empty_name_rejected_valid_still_works()
    test_location_rename_duplicate_code_rejected_unique_still_works()
    test_structure_points_exposes_what_is_missing()
    print("\nВсе тесты редактирования мест (Шаг 38) пройдены.")

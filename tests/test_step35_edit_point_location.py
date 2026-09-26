"""Шаг 35 (партия 5, задача 2): PATCH /api/v2/points/<id> теперь
принимает installation_location_id — "редактировать принадлежность" из
карточки точки (§8.3) требует уметь сменить место установки уже
существующей точки, а не только задать его при создании. До этой
партии update_fields этого поля не знал вообще (см. point_repo.py).

Самостоятельный скрипт (не pytest):
    python tests/test_step35_edit_point_location.py
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


def current_rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


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


def test_patch_point_sets_and_clears_location():
    client, db, path = make_client()
    try:
        rev = current_rev(client)
        loc = client.post("/api/v2/locations",
                           json={"name": "Место A", "kind": "room", "expected_revision": rev}).get_json()
        rev = current_rev(client)
        loc2 = client.post("/api/v2/locations",
                            json={"name": "Место B", "kind": "room", "expected_revision": rev}).get_json()

        r = client.post("/api/v2/points", json={"code": "p35.1", "name": "Точка 35.1"})
        point = r.get_json()
        assert point["installation_location_id"] is None

        rev = current_rev(client)
        r = client.patch(f"/api/v2/points/{point['id']}", json={
            "installation_location_id": loc["id"], "expected_revision": rev,
        })
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["installation_location_id"] == loc["id"]

        # легитимный повторный запрос: перенести на другое место
        rev = current_rev(client)
        r = client.patch(f"/api/v2/points/{point['id']}", json={
            "installation_location_id": loc2["id"], "expected_revision": rev,
        })
        assert r.status_code == 200
        assert r.get_json()["installation_location_id"] == loc2["id"]

        # снять место установки (null — значащее значение, не "не менять")
        rev = current_rev(client)
        r = client.patch(f"/api/v2/points/{point['id']}", json={
            "installation_location_id": None, "expected_revision": rev,
        })
        assert r.status_code == 200
        assert r.get_json()["installation_location_id"] is None
        print("[OK] PATCH точки меняет и снимает место установки")
    finally:
        db.close(); os.unlink(path)


def test_patch_point_unknown_location_rejected_but_legit_still_works():
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p35.2", "name": "Точка 35.2"})
        point_id = r.get_json()["id"]

        rev = current_rev(client)
        r = client.patch(f"/api/v2/points/{point_id}", json={
            "installation_location_id": 999999, "expected_revision": rev,
        })
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "bad_request"

        rev = current_rev(client)
        loc = client.post("/api/v2/locations",
                           json={"name": "Место C", "kind": "room", "expected_revision": rev}).get_json()
        rev = current_rev(client)
        r = client.patch(f"/api/v2/points/{point_id}", json={
            "installation_location_id": loc["id"], "expected_revision": rev,
        })
        assert r.status_code == 200, r.get_json()
        print("[OK] ОТКАЗ несуществующего места 400 + легитимный запрос проходит")
    finally:
        db.close(); os.unlink(path)


def test_patch_point_empty_body_400_not_silent_200():
    """Партия 10, этап A: тело без единого знакомого поля -> 400, ревизия
    не продвигается, точка не меняется. Раньше здесь был молчаливый 200 —
    хуже, чем у связи/узла: with_revision_check безусловно создаёт новую
    запись в истории ревизий, даже когда _mutate() ничего не сделал."""
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p35.3", "name": "Точка 35.3"})
        point_id = r.get_json()["id"]
        rev_before = current_rev(client)

        r = client.patch(f"/api/v2/points/{point_id}", json={"expected_revision": rev_before})
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "bad_request"
        assert current_rev(client) == rev_before, "пустое тело не должно продвигать ревизию"
        assert client.get(f"/api/v2/points/{point_id}").get_json()["name"] == "Точка 35.3"

        r = client.patch(f"/api/v2/points/{point_id}", json={
            "name": "Точка 35.3 (переименована)", "expected_revision": rev_before,
        })
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["name"] == "Точка 35.3 (переименована)"
        print("[OK] PATCH точки: пустое тело -> 400, ревизия не продвинута; "
              "легитимный запрос с name по-прежнему работает")
    finally:
        db.close(); os.unlink(path)


def test_patch_point_unknown_field_400_not_silent_200():
    """Тот же класс, что и пустое тело: если в теле только незнакомые
    ключи (опечатка в имени поля), результат должен быть той же 400, а
    не молчаливым 200 без изменений."""
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p35.4", "name": "Точка 35.4"})
        point_id = r.get_json()["id"]
        rev_before = current_rev(client)

        r = client.patch(f"/api/v2/points/{point_id}", json={
            "lable": "опечатка вместо label/name", "expected_revision": rev_before,
        })
        assert r.status_code == 400, r.get_json()
        assert current_rev(client) == rev_before
        assert client.get(f"/api/v2/points/{point_id}").get_json()["name"] == "Точка 35.4"

        r = client.patch(f"/api/v2/points/{point_id}", json={
            "enabled": False, "expected_revision": rev_before,
        })
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["enabled"] is False
        print("[OK] PATCH точки: незнакомое поле -> 400 без изменений; "
              "легитимный запрос с enabled по-прежнему работает")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_patch_point_sets_and_clears_location()
    test_patch_point_unknown_location_rejected_but_legit_still_works()
    test_patch_point_empty_body_400_not_silent_200()
    test_patch_point_unknown_field_400_not_silent_200()
    print("\nВсе тесты Шага 35 пройдены.")

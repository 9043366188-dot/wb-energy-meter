"""Шаг 41 (партия 7, Этап 3, Э3/B5+B8): backend-часть карточки линии
«Плана v3» — таблица теста написана в этой сессии (docs/
TZ-batch7-review-fixes.md, §B5/B8: "тест — в test_step41 (ниже)",
"таблицу пишешь ты").

Две связанные, но независимые фичи:

1. PATCH /api/v2/topology/edges/<id> дополнен полями name/
   rated_current_a/cable_note (раньше принимал только
   primary_point_id — колонки в БД и в ElectricalEdge были, но HTTP их
   не пускал вовсе). Можно сохранить любое подмножество полей одним
   запросом, включая одновременно primary_point_id И метаданные — одна
   транзакция, одна ревизия.

2. POST /api/v2/topology/edges/<id>/attach-new-point — «счётчик из
   прибора прямо на линию» одним действием: точка → привязка → счётчик
   на линию, атомарно. Идемпотентен на повтор ТОГО ЖЕ device_id на ТОЙ
   ЖЕ линии (не плодит вторую точку); отклоняет постановку НА линию,
   уже измеряемую ДРУГОЙ точкой (already_metered, 409); отклоняет
   попытку привязать прибор, уже активно измеряющий ДРУГУЮ точку
   (double_counting, 409, через тот же BindingConflict, что и
   POST /points/<id>/bindings).

Каждая проверка "отказ" — с парной "легитимный запрос работает" (см.
AGENTS.md).

Самостоятельный скрипт (не pytest):
    python tests/test_step41_planv3_api.py
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


def make_node(client, code, name, kind):
    rev = current_rev(client)
    r = client.post("/api/v2/topology/nodes", json={
        "code": code, "name": name, "kind": kind, "expected_revision": rev})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def connect(client, from_id, to_id):
    rev = current_rev(client)
    r = client.post("/api/v2/topology/edges/connect", json={
        "from_node_id": from_id, "to_node_id": to_id, "expected_revision": rev})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def make_point(client, code, name):
    rev = current_rev(client)
    r = client.post("/api/v2/points", json={
        "code": code, "name": name, "expected_revision": rev})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def bind_meter(client, point_id, device_id):
    rev = current_rev(client)
    r = client.post(f"/api/v2/points/{point_id}/bindings", json={
        "new_meter": {"controller_key": "wb8-main", "device_id": device_id,
                      "display_name": device_id},
        "channel_profile": "total_3p", "valid_from": 0, "expected_revision": rev,
    })
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def all_points(client):
    r = client.get("/api/v2/points")
    assert r.status_code == 200
    return r.get_json()


def attach(client, edge_id, **body):
    rev = current_rev(client)
    payload = {"expected_revision": rev}
    payload.update(body)
    return client.post(f"/api/v2/topology/edges/{edge_id}/attach-new-point", json=payload)


def patch_edge(client, edge_id, **body):
    rev = current_rev(client)
    payload = {"expected_revision": rev}
    payload.update(body)
    return client.patch(f"/api/v2/topology/edges/{edge_id}", json=payload)


def get_edge(client, edge_id):
    r = client.get(f"/api/v2/topology/edges/{edge_id}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# ---------------------------------------------------------------------
# B5: PATCH edges/<id> — name/rated_current_a/cable_note
# ---------------------------------------------------------------------

def test_patch_edge_accepts_name_rated_current_cable_note():
    client, db, path = make_client()
    try:
        n1 = make_node(client, "n1", "Ввод", "source")
        n2 = make_node(client, "n2", "ЩР-1", "panel")
        e = connect(client, n1["id"], n2["id"])

        r = patch_edge(client, e["id"], name="Ввод → ЩР-1",
                        rated_current_a=63.0, cable_note="ВВГнг 5x16")
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert d["name"] == "Ввод → ЩР-1", d
        assert d["rated_current_a"] == 63.0, d
        assert d["cable_note"] == "ВВГнг 5x16", d

        got = get_edge(client, e["id"])
        assert got["name"] == "Ввод → ЩР-1"
        assert got["rated_current_a"] == 63.0
        assert got["cable_note"] == "ВВГнг 5x16"
        print("[OK] PATCH edges/<id>: name/rated_current_a/cable_note сохраняются "
              "и отдаются обратно (раньше эти поля вообще не принимались)")
    finally:
        db.close(); os.unlink(path)


def test_patch_edge_combines_metadata_and_primary_point_in_one_call():
    client, db, path = make_client()
    try:
        n1 = make_node(client, "n1", "Ввод", "source")
        n2 = make_node(client, "n2", "ЩР-1", "panel")
        e = connect(client, n1["id"], n2["id"])
        p = make_point(client, "p1", "Точка на вводе")
        bind_meter(client, p["id"], "dev-001")

        rev_before = current_rev(client)
        r = patch_edge(client, e["id"], name="Ввод", cable_note="ВВГ 5x10",
                        primary_point_id=p["id"])
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert d["name"] == "Ввод" and d["cable_note"] == "ВВГ 5x10"
        assert d["primary_point_id"] == p["id"], d
        # Одна транзакция — ревизия выросла ровно на единицу, не на две
        # (метаданные и primary_point_id не расходуют её по отдельности).
        assert d["configuration_revision"] == rev_before + 1, d
        print("[OK] PATCH edges/<id>: метаданные и primary_point_id одним "
              "запросом — одна транзакция, ревизия растёт один раз")
    finally:
        db.close(); os.unlink(path)


def test_patch_edge_stale_revision_rejected_legitimate_still_works():
    client, db, path = make_client()
    try:
        n1 = make_node(client, "n1", "Ввод", "source")
        n2 = make_node(client, "n2", "ЩР-1", "panel")
        e = connect(client, n1["id"], n2["id"])
        stale = current_rev(client) - 1 if current_rev(client) > 0 else 0

        r = client.patch(f"/api/v2/topology/edges/{e['id']}", json={
            "name": "Испорченное имя", "expected_revision": -1})
        assert r.status_code == 409, r.get_json()
        got = get_edge(client, e["id"])
        assert got["name"] != "Испорченное имя"

        r = patch_edge(client, e["id"], name="Корректное имя")
        assert r.status_code == 200, r.get_json()
        assert get_edge(client, e["id"])["name"] == "Корректное имя"
        print("[OK] PATCH edges/<id>: чужая/отсутствующая ревизия -> 409 без "
              "изменений; легитимный запрос со свежей ревизией проходит")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# B8: POST edges/<id>/attach-new-point
# ---------------------------------------------------------------------

def test_attach_new_point_success_creates_point_binds_and_sets_primary():
    client, db, path = make_client()
    try:
        n1 = make_node(client, "n1", "Ввод", "source")
        n2 = make_node(client, "n2", "ЩР-1", "panel")
        e = connect(client, n1["id"], n2["id"])

        r = attach(client, e["id"], device_id="dev-attach-1")
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        assert d["primary_point_id"] == d["point"]["id"], d
        # Имя по умолчанию — имя узла-получателя (ЩР-1), §B8.
        assert d["point"]["name"] == "ЩР-1", d
        assert d["meter_source_id"] is not None

        got = get_edge(client, e["id"])
        assert got["primary_point_id"] == d["point"]["id"]
        assert len(all_points(client)) == 1
        print("[OK] attach-new-point: успех — точка создана, привязана, "
              "поставлена на линию одним вызовом; имя по умолчанию — имя узла")
    finally:
        db.close(); os.unlink(path)


def test_attach_new_point_repeat_same_device_is_idempotent_no_duplicate():
    client, db, path = make_client()
    try:
        n1 = make_node(client, "n1", "Ввод", "source")
        n2 = make_node(client, "n2", "ЩР-1", "panel")
        e = connect(client, n1["id"], n2["id"])

        r1 = attach(client, e["id"], device_id="dev-attach-2")
        assert r1.status_code == 201, r1.get_json()
        point_id = r1.get_json()["point"]["id"]
        assert len(all_points(client)) == 1

        # Повтор того же запроса (например, после сетевого сбоя и ретрая
        # с фронта) — с УЖЕ актуальной ревизией, как и должен вести себя
        # честный ретрай. Вторая точка не создаётся.
        r2 = attach(client, e["id"], device_id="dev-attach-2")
        assert r2.status_code == 200, r2.get_json()
        d2 = r2.get_json()
        assert d2["attached"] == "already", d2
        assert d2["point_id"] == point_id
        assert len(all_points(client)) == 1, "повтор не должен плодить вторую точку"
        print("[OK] attach-new-point: повтор с тем же device_id на той же линии — "
              "идемпотентен, вторая точка не создаётся")
    finally:
        db.close(); os.unlink(path)


def test_attach_new_point_rejected_when_edge_already_metered_by_other_point():
    client, db, path = make_client()
    try:
        n1 = make_node(client, "n1", "Ввод", "source")
        n2 = make_node(client, "n2", "ЩР-1", "panel")
        n3 = make_node(client, "n3", "ЩР-2", "panel")
        e = connect(client, n1["id"], n2["id"])
        e2 = connect(client, n1["id"], n3["id"])

        r1 = attach(client, e["id"], device_id="dev-attach-3a")
        assert r1.status_code == 201, r1.get_json()

        # Другой прибор на ТУ ЖЕ линию, которая уже измеряется, — отказ,
        # ничего не меняется (осознанная замена — обычный PATCH).
        rev_before = current_rev(client)
        r2 = attach(client, e["id"], device_id="dev-attach-3b")
        assert r2.status_code == 409, r2.get_json()
        assert r2.get_json()["code"] == "already_metered", r2.get_json()
        assert current_rev(client) == rev_before
        assert len(all_points(client)) == 1, "отказ не должен создавать точку"

        # Легитимно: тот же новый прибор ставится на ДРУГУЮ, ещё
        # неизмеренную линию — работает.
        r3 = attach(client, e2["id"], device_id="dev-attach-3b")
        assert r3.status_code == 201, r3.get_json()
        assert len(all_points(client)) == 2
        print("[OK] attach-new-point: повторная попытка на уже измеренную "
              "линию -> 409 already_metered без изменений; тот же прибор на "
              "другую, неизмеренную линию — легитимно работает")
    finally:
        db.close(); os.unlink(path)


def test_attach_new_point_rejected_when_device_already_bound_elsewhere():
    client, db, path = make_client()
    try:
        n1 = make_node(client, "n1", "Ввод", "source")
        n2 = make_node(client, "n2", "ЩР-1", "panel")
        n3 = make_node(client, "n3", "ЩР-2", "panel")
        e1 = connect(client, n1["id"], n2["id"])
        e2 = connect(client, n1["id"], n3["id"])

        p_existing = make_point(client, "p-existing", "Уже привязанная точка")
        bind_meter(client, p_existing["id"], "dev-busy")

        rev_before = current_rev(client)
        r = attach(client, e1["id"], device_id="dev-busy")
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "double_counting", r.get_json()
        assert current_rev(client) == rev_before
        assert len(all_points(client)) == 1, "отказ не должен создавать точку"
        got = get_edge(client, e1["id"])
        assert got["primary_point_id"] is None

        # Легитимно: свободный прибор на ту же линию — работает.
        r2 = attach(client, e1["id"], device_id="dev-free")
        assert r2.status_code == 201, r2.get_json()
        print("[OK] attach-new-point: прибор, уже активно измеряющий другую "
              "точку -> 409 double_counting без изменений; свободный прибор "
              "на ту же линию — легитимно работает")
    finally:
        db.close(); os.unlink(path)


def test_attach_new_point_404_unknown_edge_400_missing_device_id():
    client, db, path = make_client()
    try:
        n1 = make_node(client, "n1", "Ввод", "source")
        n2 = make_node(client, "n2", "ЩР-1", "panel")
        e = connect(client, n1["id"], n2["id"])

        r = attach(client, 999999, device_id="dev-x")
        assert r.status_code == 404, r.get_json()

        r = attach(client, e["id"])
        assert r.status_code == 400, r.get_json()
        assert len(all_points(client)) == 0

        r = attach(client, e["id"], device_id="dev-ok")
        assert r.status_code == 201, r.get_json()
        print("[OK] attach-new-point: 404 на несуществующую линию, 400 без "
              "device_id, ничего не создано; легитимный запрос проходит")
    finally:
        db.close(); os.unlink(path)


def test_attach_new_point_stale_revision_rejected_legitimate_still_works():
    client, db, path = make_client()
    try:
        n1 = make_node(client, "n1", "Ввод", "source")
        n2 = make_node(client, "n2", "ЩР-1", "panel")
        e = connect(client, n1["id"], n2["id"])

        r = client.post(f"/api/v2/topology/edges/{e['id']}/attach-new-point", json={
            "device_id": "dev-stale", "expected_revision": -1})
        assert r.status_code == 409, r.get_json()
        assert len(all_points(client)) == 0

        r = attach(client, e["id"], device_id="dev-stale")
        assert r.status_code == 201, r.get_json()
        print("[OK] attach-new-point: чужая/отсутствующая ревизия -> 409 без "
              "изменений; легитимный запрос со свежей ревизией проходит")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_patch_edge_accepts_name_rated_current_cable_note()
    test_patch_edge_combines_metadata_and_primary_point_in_one_call()
    test_patch_edge_stale_revision_rejected_legitimate_still_works()
    test_attach_new_point_success_creates_point_binds_and_sets_primary()
    test_attach_new_point_repeat_same_device_is_idempotent_no_duplicate()
    test_attach_new_point_rejected_when_edge_already_metered_by_other_point()
    test_attach_new_point_rejected_when_device_already_bound_elsewhere()
    test_attach_new_point_404_unknown_edge_400_missing_device_id()
    test_attach_new_point_stale_revision_rejected_legitimate_still_works()
    print("[ALL OK] test_step41_planv3_api")

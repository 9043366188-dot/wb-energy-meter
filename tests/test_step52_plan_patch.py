"""Тесты партии 11, этап 11.5: `PATCH /api/v2/plans/<id>` (дыра §9.2).

Самостоятельный скрипт (не pytest):
    python tests/test_step52_plan_patch.py

Что проверяется (после каждой записи сущность ПЕРЕЧИТЫВАЕТСЯ отдельным GET —
ответ PATCH сам по себе не доказательство, что записалось в БД):
  P1  имя меняется; ответ и перечитанный план совпадают; ревизия продвинулась;
  P2  is_default=true делает план единственным по умолчанию (у прежнего флаг
      снят — проверяется перечитыванием ОБОИХ планов);
  P3  is_default=false снимает флаг только с этого плана; другие не тронуты;
  P4  plan_kind и размеры не меняются: 400 со списком полей, план и ревизия
      не изменились; допустимый запрос сразу после отказа проходит;
  P5  тело без знакомых полей → 400 со списком допустимых полей; ревизия не
      продвинулась;
  P6  пустое/неверного типа имя и не-булев is_default → 400, ничего не записано;
  P7  устаревшая/отсутствующая expected_revision → 409, план не изменён;
  P8  неизвестный план → 404; запись в конверте {"data": {...}} работает;
  P9  у `single_line`-плана PATCH работает так же (вид не мешает).
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.db import Database
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.repo import GroupRepo, MeterRepo


def _png_bytes(width=40, height=30):
    """Минимальный PNG-заголовок (сигнатура + IHDR) — ровно то, что разбирает
    image_meta.parse_image_size(); тот же приём, что в test_step21 (Pillow в
    проекте запрещён)."""
    import struct
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    return sig + ihdr


def make_client():
    tmp = tempfile.mkdtemp(prefix="wb_plan_patch_")
    db = Database(path=os.path.join(tmp, "t.sqlite3"))
    db.open()
    groups = GroupRepo(db)
    state = _AppState(
        registry=MeterRegistry(), meters_repo=MeterRepo(db, groups),
        groups_repo=groups, is_mqtt_connected=lambda: False,
        mqtt_message_count=lambda: 0, mqtt_error_count=lambda: 0,
        wb_db_client=None, consumption_service=None, started_at=time.time(),
        db=db, plans_dir=os.path.join(tmp, "plans"))
    app = create_app(state)
    return app.test_client(), db, tmp


def current_rev(client):
    r = client.get("/api/v2/revision")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["configuration_revision"]


def create_floor(client, name):
    r = client.post("/api/v2/plans", data={
        "name": name, "plan_kind": "floor",
        "file": (io.BytesIO(_png_bytes()), "plan.png")},
        content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def create_single_line(client, name):
    r = client.post("/api/v2/plans", data={
        "name": name, "plan_kind": "single_line",
        "canvas_width": "2000", "canvas_height": "1200"},
        content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def get_plan(client, plan_id):
    r = client.get(f"/api/v2/plans/{plan_id}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def patch(client, plan_id, body):
    return client.patch(f"/api/v2/plans/{plan_id}", json=body)


def _cleanup(db, tmp):
    db.close()
    shutil.rmtree(tmp, ignore_errors=True)


def test_p1_rename_is_persisted_and_advances_revision():
    client, db, tmp = make_client()
    try:
        plan = create_floor(client, "Старое имя")
        rev = current_rev(client)

        r = patch(client, plan["id"], {"name": "  Новое имя  ", "expected_revision": rev})
        assert r.status_code == 200, r.get_json()
        out = r.get_json()
        assert out["name"] == "Новое имя", "имя нормализуется (strip), как при создании"
        assert out["configuration_revision"] > rev
        assert out["plan_kind"] == "floor"

        again = get_plan(client, plan["id"])
        assert again["name"] == "Новое имя", again
        assert again["updated_at"] >= plan["updated_at"]
        # неизменяемое осталось прежним
        for key in ("plan_kind", "image_width", "image_height", "canvas_width",
                    "canvas_height", "canvas_revision", "created_at"):
            assert again[key] == plan[key], (key, again[key], plan[key])
        assert current_rev(client) == out["configuration_revision"]
        print("[OK] P1: имя изменено и перечитано, неизменяемые поля прежние, "
              "ревизия продвинулась")
    finally:
        _cleanup(db, tmp)


def test_p2_is_default_true_makes_plan_the_only_default():
    client, db, tmp = make_client()
    try:
        first = create_floor(client, "Первый")      # первый план — по умолчанию
        second = create_floor(client, "Второй")
        assert first["is_default"] is True and second["is_default"] is False

        r = patch(client, second["id"], {
            "is_default": True, "expected_revision": current_rev(client)})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["is_default"] is True

        assert get_plan(client, second["id"])["is_default"] is True
        assert get_plan(client, first["id"])["is_default"] is False, \
            "у прежнего плана по умолчанию флаг должен быть снят"
        defaults = [p["id"] for p in client.get("/api/v2/plans").get_json()
                    if p["is_default"]]
        assert defaults == [second["id"]], defaults

        # повтор того же PATCH идемпотентен по результату (ревизия при этом
        # продвигается — это отдельная запись конфигурации)
        r2 = patch(client, second["id"], {
            "is_default": True, "expected_revision": current_rev(client)})
        assert r2.status_code == 200, r2.get_json()
        defaults = [p["id"] for p in client.get("/api/v2/plans").get_json()
                    if p["is_default"]]
        assert defaults == [second["id"]], defaults
        print("[OK] P2: is_default=true — единственный по умолчанию, "
              "прежний сброшен (проверено перечитыванием обоих)")
    finally:
        _cleanup(db, tmp)


def test_p3_is_default_false_clears_only_this_plan():
    client, db, tmp = make_client()
    try:
        first = create_floor(client, "Первый")
        second = create_floor(client, "Второй")
        third = create_floor(client, "Третий")

        r = patch(client, first["id"], {
            "is_default": False, "expected_revision": current_rev(client)})
        assert r.status_code == 200, r.get_json()
        assert get_plan(client, first["id"])["is_default"] is False
        assert get_plan(client, second["id"])["is_default"] is False
        assert get_plan(client, third["id"])["is_default"] is False

        # сначала назначаем третий, затем снимаем флаг с ВТОРОГО — третий
        # должен остаться по умолчанию
        assert patch(client, third["id"], {
            "is_default": True, "expected_revision": current_rev(client)
        }).status_code == 200
        assert patch(client, second["id"], {
            "is_default": False, "expected_revision": current_rev(client)
        }).status_code == 200
        assert get_plan(client, third["id"])["is_default"] is True
        print("[OK] P3: is_default=false снимает флаг только с этого плана")
    finally:
        _cleanup(db, tmp)


def test_p4_immutable_fields_are_rejected_with_field_list():
    client, db, tmp = make_client()
    try:
        plan = create_single_line(client, "Схема")
        before = get_plan(client, plan["id"])
        rev = current_rev(client)

        r = patch(client, plan["id"], {
            "plan_kind": "floor", "canvas_width": 10, "name": "Не должно записаться",
            "expected_revision": rev})
        assert r.status_code == 400, r.get_json()
        body = r.get_json()
        assert body["code"] == "bad_request"
        assert sorted(body["fields"]) == ["canvas_width", "plan_kind"], body
        assert body["ids"] == [plan["id"]]
        # ничего не записано: ни имя из того же тела, ни ревизия
        assert get_plan(client, plan["id"]) == before
        assert current_rev(client) == rev

        # парный контроль: после отказа легитимный запрос проходит
        ok = patch(client, plan["id"], {"name": "Схема 2", "expected_revision": rev})
        assert ok.status_code == 200, ok.get_json()
        assert get_plan(client, plan["id"])["name"] == "Схема 2"
        print("[OK] P4: plan_kind/размеры не меняются — 400 со списком полей, "
              "состояние и ревизия не тронуты; правильный запрос после отказа проходит")
    finally:
        _cleanup(db, tmp)


def test_p5_body_without_known_fields_is_400_with_allowed_fields():
    client, db, tmp = make_client()
    try:
        plan = create_floor(client, "План")
        rev = current_rev(client)
        for body in ({"expected_revision": rev},
                     {"expected_revision": rev, "colour": "red"},
                     {}):
            r = patch(client, plan["id"], body)
            assert r.status_code == 400, (body, r.get_json())
            err = r.get_json()
            assert err["code"] == "bad_request"
            assert err["fields"] == ["name", "is_default"], err
        assert current_rev(client) == rev, \
            "отказ не должен продвигать ревизию"
        assert patch(client, plan["id"], {
            "name": "План 2", "expected_revision": rev}).status_code == 200
        print("[OK] P5: тело без знакомых полей → 400 со списком name/is_default, "
              "ревизия не продвинулась")
    finally:
        _cleanup(db, tmp)


def test_p6_invalid_values_are_400_and_nothing_is_written():
    client, db, tmp = make_client()
    try:
        plan = create_floor(client, "План")
        before = get_plan(client, plan["id"])
        rev = current_rev(client)
        cases = [
            ({"name": "   "}, ["name"]),
            ({"name": ""}, ["name"]),
            ({"name": None}, ["name"]),
            ({"name": 5}, ["name"]),
            ({"name": "x" * 201}, None),       # слишком длинное — текст ошибки домена
            ({"is_default": "yes"}, ["is_default"]),
            ({"is_default": 1}, ["is_default"]),
            ({"is_default": None}, ["is_default"]),
        ]
        for body, fields in cases:
            r = patch(client, plan["id"], dict(body, expected_revision=rev))
            assert r.status_code == 400, (body, r.status_code, r.get_json())
            if fields is not None:
                assert r.get_json()["fields"] == fields, (body, r.get_json())
            assert get_plan(client, plan["id"]) == before, body
            assert current_rev(client) == rev, body
        ok = patch(client, plan["id"], {"name": "Нормальное", "expected_revision": rev})
        assert ok.status_code == 200, ok.get_json()
        print("[OK] P6: пустое/нетекстовое имя, длинное имя и не-булев "
              "is_default → 400, в БД ничего не записано")
    finally:
        _cleanup(db, tmp)


def test_p7_stale_or_missing_revision_is_409_and_plan_unchanged():
    client, db, tmp = make_client()
    try:
        plan = create_floor(client, "План")
        stale = current_rev(client)
        # кто-то другой продвинул ревизию
        assert patch(client, plan["id"], {
            "name": "Чужая правка", "expected_revision": stale}).status_code == 200
        fresh = current_rev(client)
        assert fresh > stale

        r = patch(client, plan["id"], {"name": "Моя правка", "expected_revision": stale})
        assert r.status_code == 409, r.get_json()
        body = r.get_json()
        assert body["code"] == "revision_conflict"
        assert body["fields"] == ["expected_revision"]
        assert body["ids"] == [plan["id"]]
        assert get_plan(client, plan["id"])["name"] == "Чужая правка"
        assert current_rev(client) == fresh

        r = patch(client, plan["id"], {"name": "Без ревизии"})
        assert r.status_code == 409, r.get_json()
        assert get_plan(client, plan["id"])["name"] == "Чужая правка"

        # парный контроль: с актуальной ревизией проходит
        r = patch(client, plan["id"], {"name": "Моя правка", "expected_revision": fresh})
        assert r.status_code == 200, r.get_json()
        assert get_plan(client, plan["id"])["name"] == "Моя правка"
        print("[OK] P7: устаревшая и отсутствующая expected_revision → 409, "
              "план не изменён; с актуальной ревизией проходит")
    finally:
        _cleanup(db, tmp)


def test_p8_unknown_plan_is_404_and_envelope_body_works():
    client, db, tmp = make_client()
    try:
        rev = current_rev(client)
        r = patch(client, 9999, {"name": "x", "expected_revision": rev})
        assert r.status_code == 404, r.get_json()
        assert r.get_json()["code"] == "not_found"
        assert r.get_json()["ids"] == [9999]
        assert current_rev(client) == rev

        plan = create_floor(client, "План")
        r = patch(client, plan["id"], {"data": {
            "name": "Через конверт", "expected_revision": current_rev(client)}})
        assert r.status_code == 200, r.get_json()
        assert get_plan(client, plan["id"])["name"] == "Через конверт"
        print("[OK] P8: неизвестный план → 404 (ревизия не двигалась); "
              "тело в конверте {data: …} принимается")
    finally:
        _cleanup(db, tmp)


def test_p9_single_line_plan_can_be_renamed_and_made_default():
    client, db, tmp = make_client()
    try:
        floor = create_floor(client, "Этаж")
        scheme = create_single_line(client, "Схема")
        r = patch(client, scheme["id"], {
            "name": "Схема v3", "is_default": True,
            "expected_revision": current_rev(client)})
        assert r.status_code == 200, r.get_json()
        got = get_plan(client, scheme["id"])
        assert got["name"] == "Схема v3" and got["is_default"] is True
        assert got["plan_kind"] == "single_line"
        assert got["canvas_width"] == 2000 and got["canvas_height"] == 1200
        assert get_plan(client, floor["id"])["is_default"] is False
        print("[OK] P9: single_line-план: имя и is_default меняются, "
              "вид и холст прежние")
    finally:
        _cleanup(db, tmp)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"[OK] test_step52_plan_patch: {len(tests)} тестов")

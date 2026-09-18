"""Тесты Шага 32 (партия 3, задача 1): GET /api/v2/structure/points —
поиск для экрана «Структура» по имени/коду/MQTT ID/серийнику/пути (A02).

Самостоятельный скрипт (не pytest):
    python tests/test_step32_structure_search.py"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.location_repo import LocationRepo
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.binding_service import PointBindingRepo


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


def _setup_point_with_meter_and_location(db, code, name, device_id, serial,
                                          location_name=None):
    points = MeteringPointRepo(db)
    meters = MeterRepo(db, GroupRepo(db))
    sources = MeterSourceRepo(db)
    bindings = PointBindingRepo(db)
    locations = LocationRepo(db)

    loc_id = None
    if location_name is not None:
        loc = locations.add(name=location_name, kind="room")
        loc_id = loc.id

    p = points.add(code=code, name=name, installation_location_id=loc_id)
    m = meters.add(device_id, name)
    if serial is not None:
        meters.update(device_id, serial_number=serial)
    s = sources.open_source(m.id, "wb8-main", device_id)
    bindings.open_binding(p.id, s.id, "total_3p", role="primary", valid_from=0)
    return p


def test_search_by_name_code_mqtt_serial_and_path():
    client, db, path = make_client()
    try:
        p1 = _setup_point_with_meter_and_location(
            db, "p32.1", "Насос отопления", "wb-map3e_77", "SN-12345",
            location_name="Котельная")
        p2 = _setup_point_with_meter_and_location(
            db, "p32.2", "Вентиляция цеха", "wb-map3e_88", "SN-99999",
            location_name="Цех 1")

        def ids_for(q):
            r = client.get(f"/api/v2/structure/points?q={q}")
            assert r.status_code == 200, r.get_json()
            return {x["point_id"] for x in r.get_json()["points"]}

        # A02: поиск по каждому из пяти полей отдельно находит СВОЮ точку
        # и не находит другую (без q -> обе)
        assert ids_for("") == {p1.id, p2.id}
        assert ids_for("Насос") == {p1.id}
        assert ids_for("p32.2") == {p2.id}
        assert ids_for("wb-map3e_77") == {p1.id}
        assert ids_for("SN-99999") == {p2.id}
        assert ids_for("Котельная") == {p1.id}
        # регистронезависимо
        assert ids_for("насос") == {p1.id}
        print("[OK] A02: поиск находит точку по имени/коду/MQTT ID/серийнику/пути, "
              "регистронезависимо, без ложных совпадений")
    finally:
        db.close(); os.unlink(path)


def test_unbound_and_unplaced_points_still_listed():
    """§8.3: непривязанные и неразмещённые точки не исчезают — они
    просто помечены bound=false/placed_on_plan=false, а не отсутствуют."""
    client, db, path = make_client()
    try:
        points = MeteringPointRepo(db)
        orphan = points.add(code="orphan.1", name="Ничем не привязана")

        r = client.get("/api/v2/structure/points")
        body = r.get_json()
        item = next((x for x in body["points"] if x["point_id"] == orphan.id), None)
        assert item is not None, "непривязанная точка не должна исчезать из списка"
        assert item["bound"] is False
        assert item["placed_on_plan"] is False
        assert item["meter_device_id"] is None
        assert item["location_path"] is None
        print("[OK] непривязанная и неразмещённая точка присутствует в списке "
              "(доступна, не пропадает)")
    finally:
        db.close(); os.unlink(path)


def test_location_path_reflects_full_hierarchy():
    client, db, path = make_client()
    try:
        locations = LocationRepo(db)
        points = MeteringPointRepo(db)
        building = locations.add(name="Корпус 1", kind="building")
        floor = locations.add(name="Этаж 2", kind="floor", parent_id=building.id)
        room = locations.add(name="Щитовая", kind="room", parent_id=floor.id)
        p = points.add(code="p32.3", name="Ввод щитовой",
                        installation_location_id=room.id)

        r = client.get("/api/v2/structure/points")
        item = next(x for x in r.get_json()["points"] if x["point_id"] == p.id)
        assert item["location_path"] == "Корпус 1 / Этаж 2 / Щитовая"
        print("[OK] location_path строит полный путь от корня до места установки")
    finally:
        db.close(); os.unlink(path)


def test_search_no_match_returns_empty_not_error():
    client, db, path = make_client()
    try:
        _setup_point_with_meter_and_location(
            db, "p32.4", "Точка", "wb-map3e_1", None)
        r = client.get("/api/v2/structure/points?q=совершенно-другое-слово")
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["points"] == []
        print("[OK] поиск без совпадений -> 200 с пустым списком, не ошибка")
    finally:
        db.close(); os.unlink(path)


def test_structure_points_shape_matches_frontend_usage():
    """Регрессия 18.09.2026, найдена пользователем в браузере.

    Экран «Структура» грузит пять ручек одним Promise.all. Четыре отдают
    СПИСОК, а `/api/v2/structure/points` — ОБЪЕКТ `{"points": [...]}`.
    Ответ присваивался как есть, `structPoints` становился объектом, и
    клик по точке падал с «(intermediate value).find is not a function»:
    карточка инспектора не открывалась вообще. Ниже по коду то же поле
    заполнялось правильно (`d.points||[]`), поэтому расхождение не
    бросалось в глаза.

    Проверяем оба конца контракта: форму ответа сервера и то, что фронт
    действительно достаёт `.points`, а не присваивает ответ целиком.
    """
    client, db, path = make_client()
    try:
        body = client.get("/api/v2/structure/points").get_json()
        assert isinstance(body, dict) and isinstance(body.get("points"), list), (
            "/api/v2/structure/points должен отдавать {'points': [...]}; "
            "если формат поменяли — поправьте и присваивание во фронте")
    finally:
        db.close()
        os.unlink(path)

    index_path = os.path.join(REPO_ROOT, "wb_energy_meter", "static",
                              "index.html")
    with open(index_path, encoding="utf-8") as f:
        html = f.read()
    assigns = re.findall(r"this\.structPoints\s*=\s*([^;\n]+)", html)
    assert assigns, "не нашлось ни одного присваивания structPoints"
    bad = [a.strip() for a in assigns
           if ".points" not in a and "[]" != a.strip()]
    assert not bad, (
        "structPoints присваивается ответом целиком — это снова уронит "
        "карточку инспектора с «.find is not a function». Нужно "
        "извлекать .points. Сейчас: %r" % bad)
    print("[OK] structure/points: форма ответа и её разбор во фронте согласованы")


if __name__ == "__main__":
    test_search_by_name_code_mqtt_serial_and_path()
    test_unbound_and_unplaced_points_still_listed()
    test_location_path_reflects_full_hierarchy()
    test_search_no_match_returns_empty_not_error()
    test_structure_points_shape_matches_frontend_usage()
    print("\nВсе тесты поиска экрана «Структура» (Шаг 32, партия 3, задача 1) пройдены.")

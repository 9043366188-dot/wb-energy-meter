"""Тесты Шага 11: план объекта — зоны на схеме и кабельные связи (ТЗ v0.11.0).

Самостоятельный скрипт (не pytest), без сети и без реальных картинок из
интернета — все PNG/JPEG собираются в памяти минимальными валидными
заголовками. Покрывает пункты §9 ТЗ v0.11.0 (11 штук)."""

from __future__ import annotations

import io
import json
import os
import re
import struct
import sqlite3
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import image_meta, plan_geo, plan_repo
from wb_energy_meter.api import _AppState, create_app
from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo


# ---------------------------------------------------------------------
# Генераторы минимальных валидных PNG/JPEG (без Pillow — ровно то, что
# должен уметь распарсить image_meta.py)
# ---------------------------------------------------------------------

def make_png(width, height):
    sig = image_meta.PNG_SIGNATURE
    ihdr_data = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    return sig + ihdr


def make_jpeg(width, height):
    comp = bytes([1, 0x11, 0])  # 1 компонент: id=1, sampling=0x11, qtable=0
    sof_body = struct.pack(">BHHB", 8, height, width, 1) + comp
    sof = b"\xff\xc0" + struct.pack(">H", len(sof_body) + 2) + sof_body
    return b"\xff\xd8" + sof + b"\xff\xd9"


GIF_BYTES = b"GIF89a" + b"\x00" * 20  # чужая сигнатура, переименованная в .png


# ---------------------------------------------------------------------
# 1. Разбор размеров PNG и JPEG стандартной библиотекой
# ---------------------------------------------------------------------

def test_parse_png_ok():
    data = make_png(800, 600)
    fmt, w, h = image_meta.parse_image_size(data)
    assert fmt == "png" and w == 800 and h == 600
    print("[OK] PNG: размеры разобраны верно")


def test_parse_jpeg_ok():
    data = make_jpeg(640, 480)
    fmt, w, h = image_meta.parse_image_size(data)
    assert fmt == "jpeg" and w == 640 and h == 480
    print("[OK] JPEG: размеры разобраны верно")


def test_parse_png_corrupted():
    # Достаточно длинный файл (>=24 байт), но на месте IHDR — мусор.
    data = image_meta.PNG_SIGNATURE + b"\x00\x00\x00\x0d" + b"XXXX" + b"\x00" * 12
    try:
        image_meta.parse_png_size(data)
        assert False, "должен был упасть"
    except image_meta.ImageFormatError as e:
        assert "IHDR" in str(e)
    print("[OK] битый PNG -> внятная ошибка")


def test_parse_jpeg_corrupted():
    # SOF-маркер объявлен, но данные обрезаны
    data = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00"
    try:
        image_meta.parse_jpeg_size(data)
        assert False, "должен был упасть"
    except image_meta.ImageFormatError as e:
        assert "обрезан" in str(e) or "SOF" in str(e)
    print("[OK] битый JPEG -> внятная ошибка")


# ---------------------------------------------------------------------
# 2. Отказ на файле с чужой сигнатурой; 3. отказ на превышении лимита
# 4. имя файла на диске генерируем мы (защита от обхода каталога)
# ---------------------------------------------------------------------

def _make_app(tmpdir):
    dbpath = os.path.join(tmpdir, "state.db")
    db = Database(path=dbpath)
    db.open()
    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)
    prepo = plan_repo.SitePlanRepo(db)
    zrepo = plan_repo.PlanZoneRepo(db)
    lrepo = plan_repo.PlanLinkRepo(db)
    pdir = plan_repo.plans_dir(dbpath)

    class FakeRegistry:
        def __init__(self): self._m = {}
        def all(self): return list(self._m.values())
        def get(self, d): return self._m.get(d)
        def put(self, device_id, state_obj): self._m[device_id] = state_obj
        def apply_registry_config(self, entries): pass  # не нужен реестру в тестах
        def set_group(self, device_id, group): pass

    registry = FakeRegistry()
    state = _AppState(
        registry=registry, meters_repo=meters_repo, groups_repo=groups_repo,
        is_mqtt_connected=lambda: True, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None,
        consumption_service=None, started_at=0,
        plan_repo=prepo, plan_zone_repo=zrepo, plan_link_repo=lrepo,
        plans_dir=pdir,
    )
    app = create_app(state)
    return app, db, groups_repo, meters_repo, prepo, zrepo, lrepo, pdir, registry


def test_upload_rejects_wrong_signature():
    with tempfile.TemporaryDirectory() as tmp:
        app, db, *_rest, pdir = _make_app(tmp)[:8]
        c = app.test_client()
        r = c.post("/api/plans", data={
            "name": "Тест",
            "file": (io.BytesIO(GIF_BYTES), "plan.png"),
        }, content_type="multipart/form-data")
        assert r.status_code == 400, r.get_data(as_text=True)
        # Файл не должен был сохраниться нигде.
        assert not os.path.isdir(pdir) or os.listdir(pdir) == []
        db.close()
    print("[OK] загрузка с чужой сигнатурой отклонена (400), файл не сохранён")


def test_upload_rejects_oversized():
    with tempfile.TemporaryDirectory() as tmp:
        app, db, *_rest, pdir = _make_app(tmp)[:8]
        c = app.test_client()
        big = image_meta.PNG_SIGNATURE + b"\x00" * (plan_repo.MAX_UPLOAD_BYTES + 1000)
        r = c.post("/api/plans", data={
            "name": "Большой",
            "file": (io.BytesIO(big), "big.png"),
        }, content_type="multipart/form-data")
        assert r.status_code in (413, 400), r.status_code
        assert not os.path.isdir(pdir) or os.listdir(pdir) == []
        db.close()
    print("[OK] загрузка больше 10 МБ отклонена, файл не сохранён")


def test_upload_filename_never_used_for_path():
    with tempfile.TemporaryDirectory() as tmp:
        app, db, *_rest, pdir = _make_app(tmp)[:8]
        c = app.test_client()
        png = make_png(100, 50)
        evil_names = [
            "../../../../etc/passwd",
            "..\\..\\windows\\system32\\evil.png",
            "/etc/passwd",
            "plan_1.png\x00.jpg",
        ]
        for evil in evil_names:
            r = c.post("/api/plans", data={
                "name": "Обход каталога",
                "file": (io.BytesIO(png), evil),
            }, content_type="multipart/form-data")
            assert r.status_code == 201, (evil, r.get_data(as_text=True))
            body = r.get_json()
            plan_id = body["id"]
            # На диске лежит РОВНО наш файл, ничего похожего на evil-имя.
            files = os.listdir(pdir)
            assert f"plan_{plan_id}.png" in files
            assert not any(".." in f or "etc" in f or "passwd" in f for f in files)
        # За пределами каталога планов ничего не появилось.
        assert not os.path.exists("/etc/passwd.evil")
        db.close()
    print("[OK] имя загруженного файла нигде не используется как путь "
          "(обход каталога невозможен)")


# ---------------------------------------------------------------------
# 5. Валидация геометрии
# ---------------------------------------------------------------------

def test_geometry_validation():
    # < 3 точек
    try:
        plan_geo.validate_geometry([[0, 0], [1, 1]], "polygon", 100, 100)
        assert False
    except ValueError:
        pass
    # координаты за границами
    try:
        plan_geo.validate_geometry([[0, 0], [0, 200], [200, 200]], "polygon", 100, 100)
        assert False
    except ValueError:
        pass
    # NaN / Infinity
    try:
        plan_geo.validate_geometry([[0, 0], [0, float("nan")], [10, 10]],
                                   "polygon", 100, 100)
        assert False
    except ValueError:
        pass
    try:
        plan_geo.validate_geometry([[0, 0], [0, float("inf")], [10, 10]],
                                   "polygon", 100, 100)
        assert False
    except ValueError:
        pass
    # не-числа
    try:
        plan_geo.validate_geometry([[0, 0], ["a", "b"], [10, 10]],
                                   "polygon", 100, 100)
        assert False
    except ValueError:
        pass
    # валидный треугольник — не падает
    plan_geo.validate_geometry([[0, 0], [0, 10], [10, 10]], "polygon", 100, 100)
    print("[OK] валидация геометрии: <3 точек, границы, NaN/Infinity, "
          "не-числа — всё отклоняется")


def test_geometry_validation_via_api_rejects_and_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        app, db, groups_repo, meters_repo, prepo, zrepo, *_rest = _make_app(tmp)[:8]
        c = app.test_client()
        png = make_png(100, 100)
        r = c.post("/api/plans", data={"name": "П", "file": (io.BytesIO(png), "a.png")},
                   content_type="multipart/form-data")
        plan_id = r.get_json()["id"]
        g = groups_repo.create("Зона А")

        bad_bodies = [
            {"geometry": [[0, 0], [1, 1]]},                      # < 3 точек
            {"geometry": [[0, 0], [0, 200], [50, 50]]},           # за границей
            {"geometry": [[0, 0], [0, "x"], [10, 10]]},           # не число
        ]
        for body in bad_bodies:
            r = c.put(f"/api/plans/{plan_id}/zones/{g.id}",
                      data=json.dumps(body), content_type="application/json")
            assert r.status_code == 400, (body, r.get_data(as_text=True))
        assert zrepo.get(plan_id, g.id) is None
        db.close()
    print("[OK] невалидная геометрия через API -> 400, в БД ничего не записано")


# ---------------------------------------------------------------------
# 6. Конвертеры координат
# ---------------------------------------------------------------------

def test_pixel_leaflet_roundtrip():
    cases = [(0, 0), (100, 50), (0, 999), (999, 0), (12.5, 7.25)]
    for x, y in cases:
        ll = plan_geo.pixel_to_leaflet(x, y)
        x2, y2 = plan_geo.leaflet_to_pixel(ll)
        assert abs(x2 - x) < 1e-9 and abs(y2 - y) < 1e-9, (x, y, ll, x2, y2)
    # прямое соответствие: pixel(x,y) -> [y,x] (не [x,y])
    assert plan_geo.pixel_to_leaflet(3, 7) == [7.0, 3.0]
    print("[OK] конвертеры пиксели<->[y,x] Leaflet — прямое и обратное "
          "преобразование, включая границы")


# ---------------------------------------------------------------------
# 7. Валидация связей
# ---------------------------------------------------------------------

def test_link_validation():
    with tempfile.TemporaryDirectory() as tmp:
        app, db, groups_repo, meters_repo, prepo, zrepo, lrepo, pdir = _make_app(tmp)[:8]
        c = app.test_client()
        png = make_png(100, 100)
        r = c.post("/api/plans", data={"name": "П", "file": (io.BytesIO(png), "a.png")},
                   content_type="multipart/form-data")
        plan_id = r.get_json()["id"]
        g1 = groups_repo.create("Зона 1")
        g2 = groups_repo.create("Зона 2")
        geom = [[0, 0], [0, 10], [10, 10]]
        c.put(f"/api/plans/{plan_id}/zones/{g1.id}",
             data=json.dumps({"geometry": geom}), content_type="application/json")
        c.put(f"/api/plans/{plan_id}/zones/{g2.id}",
             data=json.dumps({"geometry": geom}), content_type="application/json")
        z1 = zrepo.get(plan_id, g1.id)
        z2 = zrepo.get(plan_id, g2.id)

        # from == to
        r = c.post(f"/api/plans/{plan_id}/links",
                   data=json.dumps({"from_zone_id": z1.id, "to_zone_id": z1.id}),
                   content_type="application/json")
        assert r.status_code == 400, r.get_data(as_text=True)

        # зона из чужого плана
        png2 = make_png(50, 50)
        r2 = c.post("/api/plans", data={"name": "Другой", "file": (io.BytesIO(png2), "b.png")},
                    content_type="multipart/form-data")
        other_plan_id = r2.get_json()["id"]
        g3 = groups_repo.create("Зона 3")
        c.put(f"/api/plans/{other_plan_id}/zones/{g3.id}",
             data=json.dumps({"geometry": [[0, 0], [0, 5], [5, 5]]}),
             content_type="application/json")
        z3 = zrepo.get(other_plan_id, g3.id)
        r = c.post(f"/api/plans/{plan_id}/links",
                   data=json.dumps({"from_zone_id": z1.id, "to_zone_id": z3.id}),
                   content_type="application/json")
        assert r.status_code == 400, r.get_data(as_text=True)

        # отрицательный rated_current_a
        r = c.post(f"/api/plans/{plan_id}/links",
                   data=json.dumps({"from_zone_id": z1.id, "to_zone_id": z2.id,
                                     "rated_current_a": -5}),
                   content_type="application/json")
        assert r.status_code == 400, r.get_data(as_text=True)

        # валидная связь проходит
        r = c.post(f"/api/plans/{plan_id}/links",
                   data=json.dumps({"from_zone_id": z1.id, "to_zone_id": z2.id,
                                     "rated_current_a": 63}),
                   content_type="application/json")
        assert r.status_code == 201, r.get_data(as_text=True)
        db.close()
    print("[OK] валидация связи: from==to -> 400, чужая зона -> 400, "
          "отрицательный rated_current_a -> 400")


# ---------------------------------------------------------------------
# 8. Каскад удаления
# ---------------------------------------------------------------------

def test_cascade_delete_plan_and_group():
    with tempfile.TemporaryDirectory() as tmp:
        app, db, groups_repo, meters_repo, prepo, zrepo, lrepo, pdir = _make_app(tmp)[:8]
        c = app.test_client()
        png = make_png(100, 100)
        r = c.post("/api/plans", data={"name": "П", "file": (io.BytesIO(png), "a.png")},
                   content_type="multipart/form-data")
        plan_id = r.get_json()["id"]
        g1 = groups_repo.create("Цех 1")
        g2 = groups_repo.create("Цех 2")
        geom = [[0, 0], [0, 10], [10, 10]]
        c.put(f"/api/plans/{plan_id}/zones/{g1.id}",
             data=json.dumps({"geometry": geom}), content_type="application/json")
        c.put(f"/api/plans/{plan_id}/zones/{g2.id}",
             data=json.dumps({"geometry": geom}), content_type="application/json")
        z1 = zrepo.get(plan_id, g1.id)
        z2 = zrepo.get(plan_id, g2.id)
        c.post(f"/api/plans/{plan_id}/links",
              data=json.dumps({"from_zone_id": z1.id, "to_zone_id": z2.id}),
              content_type="application/json")

        # Добавим счётчик в Цех 1 через реестр — проверим, что после
        # удаления группы он не пострадает, только потеряет привязку к зоне.
        meters_repo.add(device_id="wb-map3e_1", display_name="Счётчик 1",
                        group="Цех 1")

        # 8а: удаление зоны (группы) через СУЩЕСТВУЮЩИЙ API уносит её
        # зону с плана (и связь, которая на неё ссылалась).
        r = c.delete(f"/api/registry/groups/{g1.id}")
        assert r.status_code == 200, r.get_data(as_text=True)
        assert zrepo.get(plan_id, g1.id) is None
        remaining_zones = zrepo.list_by_plan(plan_id)
        assert [z.group_id for z in remaining_zones] == [g2.id]
        links_left = lrepo.list_by_plan(plan_id)
        assert links_left == [], "связь, ссылавшаяся на удалённую зону, должна была уйти каскадом"

        meter = meters_repo.get_by_device_id("wb-map3e_1")
        assert meter is not None, "счётчик должен остаться в реестре"
        assert meter.group_id is None, "счётчик должен потерять привязку к зоне"

        # 8б: удаление плана уносит все зоны и связи.
        r = c.delete(f"/api/plans/{plan_id}")
        assert r.status_code == 200
        assert zrepo.list_by_plan(plan_id) == []
        assert lrepo.list_by_plan(plan_id) == []
        assert prepo.get_by_id(plan_id) is None
        # файл картинки тоже удалён
        assert os.listdir(pdir) == [] or all(
            not f.startswith(f"plan_{plan_id}.") for f in os.listdir(pdir))
        db.close()
    print("[OK] каскад: удаление плана уносит зоны и связи; удаление "
          "группы уносит её зону с плана, реестр счётчиков цел")


# ---------------------------------------------------------------------
# 9. /live без счётчиков и без данных — 200 с null-ами
# ---------------------------------------------------------------------

def test_live_no_meters_no_data():
    with tempfile.TemporaryDirectory() as tmp:
        app, db, groups_repo, meters_repo, prepo, zrepo, lrepo, pdir = _make_app(tmp)[:8]
        c = app.test_client()

        # План вообще без зон.
        png = make_png(100, 100)
        r = c.post("/api/plans", data={"name": "Пустой", "file": (io.BytesIO(png), "a.png")},
                   content_type="multipart/form-data")
        plan_id = r.get_json()["id"]
        r = c.get(f"/api/plans/{plan_id}/live?period=today")
        assert r.status_code == 200
        d = r.get_json()
        assert d == {"zones": [], "links": []}

        # Зона есть, но у группы нет счётчиков.
        g = groups_repo.create("Без счётчиков")
        c.put(f"/api/plans/{plan_id}/zones/{g.id}",
             data=json.dumps({"geometry": [[0, 0], [0, 10], [10, 10]]}),
             content_type="application/json")
        r = c.get(f"/api/plans/{plan_id}/live?period=today")
        assert r.status_code == 200
        d = r.get_json()
        assert len(d["zones"]) == 1
        z = d["zones"][0]
        assert z["group_id"] == g.id
        assert z["meters_total"] == 0
        assert z["power_kw"] is None
        assert z["consumption_kwh"] is None
        assert z["worst_status"] is None
        db.close()
    print("[OK] /api/plans/<id>/live без счётчиков и без данных -> 200 с null-ами")


# ---------------------------------------------------------------------
# 10. Обход каталога через маршрут статики
# ---------------------------------------------------------------------

def test_static_vendor_traversal_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        app, db, *_rest = _make_app(tmp)[:8]
        c = app.test_client()
        for path in (
            "/static/vendor/../../../../etc/passwd",
            "/static/vendor/..%2f..%2f..%2fetc%2fpasswd",
            "/static/vendor/....//....//etc/passwd",
        ):
            r = c.get(path)
            assert r.status_code == 404, (path, r.status_code)
            assert b"root:" not in r.data
        db.close()
    print("[OK] обход каталога через /static/vendor/... -> 404, файл не отдан")


def test_static_vendor_serves_every_referenced_file():
    """Регрессия 09.09.2026 (белый экран на боевом контроллере).

    Маршрут делал `from werkzeug.utils import safe_join`, но в Werkzeug
    1.0.1 (Debian bullseye, python3-werkzeug на Wiren Board) эта функция
    лежит в `werkzeug.security` — в `werkzeug.utils` она появилась только
    во 2.0. На контроллере импорт падал, обработчик отдавал 500,
    `alpine.min.js` не грузился, Alpine не стартовал. Итог: вкладки на
    `<template x-if>` не рендерились вовсе, вкладка «План» на `x-show`
    висела всегда, а `:data-theme` не проставлялся — страница вообще без
    стилей. Ни одной ошибки в логе сервиса при этом не видно.

    Почему прошлый тест это не поймал: он проверял только, что попытки
    обхода каталога дают 404. При сломанном импорте они и давали 404 —
    просто по другой причине. Что легитимный файл реально отдаётся, не
    проверял никто.

    Поэтому здесь проверяется ровно обратное: КАЖДЫЙ файл, на который
    ссылается index.html, отдаётся с кодом 200 и непустым телом. Тест
    заодно ловит ситуацию «добавили <script src>, а вендорить забыли».
    """
    index_path = os.path.join(
        REPO_ROOT, "wb_energy_meter", "static", "index.html")
    with open(index_path, encoding="utf-8") as f:
        html = f.read()

    refs = sorted(set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html)))
    assert refs, "в index.html не нашлось ни одной ссылки на /static/ — " \
                 "проверка бессмысленна, поправьте тест"

    with tempfile.TemporaryDirectory() as tmp:
        app, db, *_rest = _make_app(tmp)[:8]
        c = app.test_client()
        for ref in refs:
            r = c.get(ref)
            assert r.status_code == 200, (
                "%s -> %s (на контроллере это белый экран без ошибок "
                "в логе)" % (ref, r.status_code))
            assert len(r.data) > 0, ref
            ctype = r.headers.get("Content-Type", "")
            if ref.endswith(".js"):
                assert "javascript" in ctype, (ref, ctype)
            elif ref.endswith(".css"):
                assert "css" in ctype, (ref, ctype)
        db.close()
    print("[OK] все %d файла из index.html отдаются с кодом 200" % len(refs))


def test_plan_upload_form_reachable_when_no_plans():
    """Регрессия 09.09.2026: «курица и яйцо» на пустой вкладке «План».

    Форма загрузки плана лежит внутри блока, который показывался по
    условию `x-show="planPlans.length>0"`. Кнопка «Загрузить план» из
    пустого состояния ставила `planEditMode=true`, но блок оставался
    скрыт — планов-то по-прежнему ноль. На свежей установке вкладка
    выглядела так: одна неработающая кнопка и ни одного элемента
    управления, загрузить первый план физически нечем.

    Проверяем ровно суть: блок, содержащий форму загрузки, обязан
    показываться и когда планов нет, но включён режим редактирования.
    """
    index_path = os.path.join(
        REPO_ROOT, "wb_energy_meter", "static", "index.html")
    with open(index_path, encoding="utf-8") as f:
        html = f.read()

    assert 'x-ref="planFileInput"' in html, \
        "не нашёлся input[type=file] формы загрузки плана — поправьте тест"

    # Условие показа блока-обёртки, внутри которого лежит форма загрузки.
    m = re.search(r'<div x-show="planPlans\.length>0([^"]*)"', html)
    assert m, ("не нашёлся блок вкладки «План» с x-show по planPlans.length "
               "— если разметку переписали, обновите тест")
    condition = m.group(1)
    assert "planEditMode" in condition, (
        "блок с формой загрузки плана показывается только при "
        "planPlans.length>0 — значит на свежей установке (планов нет) "
        "форма недостижима и первый план загрузить нельзя. "
        "Условие должно включать planEditMode. Сейчас: %r" % condition)

    # Пустое состояние обязано прятаться при входе в режим редактирования,
    # иначе заглушка «Планов пока нет» перекрывает открывшуюся форму.
    m2 = re.search(
        r'<template x-if="!planLoading && planPlans\.length===0([^"]*)"', html)
    assert m2 and "planEditMode" in m2.group(1), (
        "пустое состояние вкладки «План» не учитывает planEditMode — "
        "оно останется на экране поверх формы загрузки")
    print("[OK] форма загрузки плана достижима, когда планов ещё нет")


# ---------------------------------------------------------------------
# 11. Миграция 004 на БД с уже существующими данными (v0.10.0)
# ---------------------------------------------------------------------

def test_migration_004_on_existing_db():
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "wb_energy_meter", "migrations")
    with tempfile.TemporaryDirectory() as tmp:
        dbpath = os.path.join(tmp, "state.db")

        # Шаг 1: вручную поднимаем БД строго на миграциях 001-003 —
        # имитация «уже стоит v0.10.0» — и кладём туда данные.
        conn = sqlite3.connect(dbpath)
        conn.execute("PRAGMA foreign_keys = ON")

        def _py_casefold(s):
            return None if s is None else str(s).strip().casefold()
        conn.create_function("py_casefold", 1, _py_casefold)

        for name in ("001_initial_schema.sql", "002_aggregator_indexes.sql",
                     "003_group_name_normalized.sql"):
            sql = open(os.path.join(migrations_dir, name), encoding="utf-8").read()
            conn.executescript(sql)

        conn.execute("INSERT INTO meter_groups (name, name_norm, parent_id, color, "
                    "created_at) VALUES ('Цех 1', 'цех 1', NULL, '#2f9e8f', 1700000000)")
        gid = conn.execute("SELECT id FROM meter_groups WHERE name='Цех 1'").fetchone()[0]
        conn.execute("INSERT INTO meters (device_id, display_name, group_id, role, "
                    "enabled, notes, created_at, updated_at) VALUES "
                    "('wb-map3e_1', 'Счётчик 1', ?, 'consumer', 1, NULL, "
                    "1700000000, 1700000000)", (gid,))
        conn.commit()
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3
        conn.close()

        # Шаг 2: открываем той же Database (уже с миграцией 004 в комплекте) —
        # должна доехать до версии 4, ничего не потеряв.
        db = Database(path=dbpath)
        db.open()
        assert db.current_schema_version() == 4

        row = db.conn().execute(
            "SELECT display_name, group_id FROM meters WHERE device_id='wb-map3e_1'"
        ).fetchone()
        assert row is not None
        assert row["display_name"] == "Счётчик 1"
        assert row["group_id"] == gid

        row = db.conn().execute(
            "SELECT name FROM meter_groups WHERE id=?", (gid,)).fetchone()
        assert row["name"] == "Цех 1"

        # Новые таблицы существуют и пусты.
        for t in ("site_plans", "plan_zones", "plan_links"):
            n = db.conn().execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            assert n == 0, t

        # И миграцию можно применить второй раз (повторный запуск сервиса)
        # без ошибок — current_schema_version уже 4, значит она просто
        # пропускается (см. db.py::_apply_migrations: version <= current).
        db.close()
        db2 = Database(path=dbpath)
        db2.open()
        assert db2.current_schema_version() == 4
        db2.close()
    print("[OK] миграция 004 применяется на БД с данными v0.10.0 без потерь")


if __name__ == "__main__":
    test_parse_png_ok()
    test_parse_jpeg_ok()
    test_parse_png_corrupted()
    test_parse_jpeg_corrupted()
    test_upload_rejects_wrong_signature()
    test_upload_rejects_oversized()
    test_upload_filename_never_used_for_path()
    test_geometry_validation()
    test_geometry_validation_via_api_rejects_and_writes_nothing()
    test_pixel_leaflet_roundtrip()
    test_link_validation()
    test_cascade_delete_plan_and_group()
    test_live_no_meters_no_data()
    test_static_vendor_traversal_blocked()
    test_static_vendor_serves_every_referenced_file()
    test_plan_upload_form_reachable_when_no_plans()
    test_migration_004_on_existing_db()
    print("\nВсе тесты Шага 11 (план объекта) пройдены.")

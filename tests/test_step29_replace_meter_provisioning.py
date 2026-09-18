"""Тесты Шага 29 (партия 3, задача 3): провижининг нового прибора прямо
внутри POST /api/v2/points/<id>/replace-meter (ТЗ §5.4).

Самостоятельный скрипт (не pytest):
    python tests/test_step29_replace_meter_provisioning.py

Пишется и проверяется лично (не делегировано) — маршрут финансово
критичен (создаёт meter+meter_source и сегментирует привязку в одной
транзакции с ревизией), см. AGENTS.md — конвенция проекта.

Методология партии 3 (docs/TZ-batch3-structure-inspector-legacy.md §5):
у каждой проверки «отказ» есть парная проверка «легитимный запрос
работает» — тест, проверяющий только отказ, уже один раз пропустил
поломку целого маршрута (см. AGENTS.md, "обход каталога даёт 404")."""

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
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.binding_service import PointBindingRepo

HOUR = 3600


def current_rev(client):
    r = client.get("/api/v2/revision")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["configuration_revision"]


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


def test_new_meter_provisioning_creates_meter_and_source_and_replaces():
    """Основной позитивный сценарий партии 3, задача 3: тело с
    new_meter создаёт meter+meter_source и сегментирует привязку,
    ничем не отличаясь по итогу от старого варианта с готовым
    meter_source_id."""
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p29.1", "name": "Точка 29.1"})
        point_id = r.get_json()["id"]

        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        bindings = PointBindingRepo(db)
        old_meter = meters.add("old-dev-1", "Старый прибор")
        old_source = sources.open_source(old_meter.id, "wb8-main", "old-dev-1")
        bindings.open_binding(point_id, old_source.id, "total_3p", role="primary",
                               valid_from=0)

        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/replace-meter", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": "new-dev-1",
                          "display_name": "Новый прибор"},
            "at": HOUR, "expected_revision": rev,
        })
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["point_id"] == point_id
        assert isinstance(body["configuration_revision"], int)
        new_source_id = body["meter_source_id"]
        assert new_source_id != old_source.id

        # meter реально создан и найден по device_id, а не потерян
        created_meter = meters.get_by_device_id("new-dev-1")
        assert created_meter is not None
        created_source = sources.get_by_id(new_source_id)
        assert created_source.meter_id == created_meter.id
        assert created_source.controller_key == "wb8-main"
        assert created_source.device_id == "new-dev-1"

        # старый интервал завершён сервером САМ (не задваиваем закрытие)
        old_bindings = [b for b in bindings.list_for_point(point_id)]
        closed = [b for b in old_bindings if b.meter_source_id == old_source.id]
        assert len(closed) == 1 and closed[0].valid_to == HOUR
        opened = [b for b in old_bindings if b.meter_source_id == new_source_id]
        assert len(opened) == 1 and opened[0].valid_from == HOUR and opened[0].valid_to is None
        print("[OK] new_meter в теле replace-meter создаёт meter+meter_source и "
              "сегментирует привязку (позитивный путь партии 3, задача 3)")
    finally:
        db.close(); os.unlink(path)


def test_new_meter_reuses_existing_meter_by_device_id():
    """Повторное использование ФИЗИЧЕСКОГО прибора: если device_id уже
    существует в meters (например, прибор сняли с одной точки и ставят
    на другую) — новую строку meters заводить не нужно, узнаём его по
    device_id."""
    client, db, path = make_client()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        existing_meter = meters.add("known-dev", "Известный прибор")

        r = client.post("/api/v2/points", json={"code": "p29.2", "name": "Точка 29.2"})
        point_id = r.get_json()["id"]
        sources = MeterSourceRepo(db)
        bindings = PointBindingRepo(db)
        # начальная привязка через отдельный (заведомо другой) источник,
        # чтобы замена была настоящей заменой, а не первым назначением
        placeholder_meter = meters.add("placeholder-dev", "Заглушка")
        placeholder_source = sources.open_source(placeholder_meter.id, "wb8-main",
                                                  "placeholder-dev")
        bindings.open_binding(point_id, placeholder_source.id, "total_3p",
                               role="primary", valid_from=0)

        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/replace-meter", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": "known-dev"},
            "expected_revision": rev,
        })
        assert r.status_code == 200, r.get_json()
        new_source_id = r.get_json()["meter_source_id"]
        new_source = sources.get_by_id(new_source_id)
        assert new_source.meter_id == existing_meter.id, (
            "должен переиспользовать существующий meter по device_id, а не "
            "плодить дубль")
        # meters содержит ровно тот же прибор, без дублей по device_id
        assert meters.get_by_device_id("known-dev").id == existing_meter.id
        print("[OK] new_meter с уже известным device_id переиспользует meter "
              "(не плодит дубль)")
    finally:
        db.close(); os.unlink(path)


def test_new_meter_retry_is_idempotent_no_duplicate_source():
    """Повтор ТОГО ЖЕ запроса (например, клиент не увидел ответ из-за
    сетевого сбоя и повторил) не должен плодить лишний meter_source —
    источник для этого meter уже открыт именно на этот адрес."""
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p29.3", "name": "Точка 29.3"})
        point_id = r.get_json()["id"]
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        bindings = PointBindingRepo(db)
        old_meter = meters.add("old-dev-3", "Старый")
        old_source = sources.open_source(old_meter.id, "wb8-main", "old-dev-3")
        bindings.open_binding(point_id, old_source.id, "total_3p", role="primary",
                               valid_from=0)

        rev = current_rev(client)
        r1 = client.post(f"/api/v2/points/{point_id}/replace-meter", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": "new-dev-3"},
            "at": 100, "expected_revision": rev,
        })
        assert r1.status_code == 200, r1.get_json()
        source_id_1 = r1.get_json()["meter_source_id"]
        new_meter = sources.get_by_id(source_id_1).meter_id
        sources_before = sources.list_for_meter(new_meter)
        assert len(sources_before) == 1

        # ПОВТОР: та же точка, та же new_meter-заявка, свежая ревизия
        # (это уже другой домен-запрос по протоколу ревизий, но по
        # адресу прибора — тот же самый физический прибор)
        rev2 = current_rev(client)
        r2 = client.post(f"/api/v2/points/{point_id}/replace-meter", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": "new-dev-3"},
            "at": 200, "expected_revision": rev2,
        })
        assert r2.status_code == 200, r2.get_json()
        source_id_2 = r2.get_json()["meter_source_id"]
        assert source_id_2 == source_id_1, (
            "тот же адрес того же прибора должен переиспользовать открытый "
            "источник, а не создавать новый")
        sources_after = sources.list_for_meter(new_meter)
        assert len(sources_after) == 1, "повтор не должен плодить лишний meter_source"
        print("[OK] повтор new_meter с тем же адресом идемпотентен — "
              "meter_source не дублируется")
    finally:
        db.close(); os.unlink(path)


def test_new_meter_address_taken_by_another_meter_rejected():
    """ОТКАЗ: адрес (controller_key, device_id), уже открытый у ДРУГОГО
    прибора, не может быть переприсвоен новому — иначе один и тот же
    физический канал измерялся бы как два разных прибора."""
    client, db, path = make_client()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        bindings = PointBindingRepo(db)
        # Физический прибор, чей ЕСТЕСТВЕННЫЙ ключ (meters.device_id) —
        # "busy-meter-key", но его действующий канал измерения
        # (meter_sources.device_id — реальный адрес MQTT) — "busy-dev".
        # Это и есть настоящий конфликт адреса: другой meter с ДРУГИМ
        # meters.device_id уже занял именно этот (controller_key,
        # device_id) в meter_sources.
        busy_meter = meters.add("busy-meter-key", "Занятый")
        sources.open_source(busy_meter.id, "wb8-main", "busy-dev")

        r = client.post("/api/v2/points", json={"code": "p29.4", "name": "Точка 29.4"})
        point_id = r.get_json()["id"]
        old_meter = meters.add("old-dev-4", "Старый")
        old_source = sources.open_source(old_meter.id, "wb8-main", "old-dev-4")
        bindings.open_binding(point_id, old_source.id, "total_3p", role="primary",
                               valid_from=0)

        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/replace-meter", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": "busy-dev"},
            "expected_revision": rev,
        })
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "bad_request"
        # Атомарность: попытка создать новый meter с device_id="busy-dev"
        # НЕ должна была осесть в базе после отката всей транзакции —
        # иначе после нескольких неудачных попыток замены накопился бы
        # мусор в meters (партия 2: одна внешняя транзакция на всё).
        assert meters.get_by_device_id("busy-dev") is None, (
            "meter не должен был осесть в БД после отката конфликтной транзакции")
        print("[OK] ОТКАЗ: адрес, занятый другим прибором, отклонён 400 "
              "(и транзакция откатилась целиком, включая пробную запись meters)")
    finally:
        db.close(); os.unlink(path)


def test_new_meter_missing_fields_rejected_but_legacy_path_still_works():
    """Парная проверка к предыдущей: тело без meter_source_id и без
    new_meter отклоняется 400 — а старый вариант (готовый
    meter_source_id) по-прежнему работает через тот же маршрут (не
    сломан добавлением new_meter)."""
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p29.5", "name": "Точка 29.5"})
        point_id = r.get_json()["id"]
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        bindings = PointBindingRepo(db)
        m1 = meters.add("m1-dev", "Прибор 1")
        s1 = sources.open_source(m1.id, "wb8-main", "m1-dev")
        bindings.open_binding(point_id, s1.id, "total_3p", role="primary", valid_from=0)

        # ОТКАЗ: пустое тело
        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/replace-meter",
                         json={"expected_revision": rev})
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "bad_request"

        # ЛЕГИТИМНЫЙ ЗАПРОС РАБОТАЕТ: старый вариант с meter_source_id
        # (регрессия — маршрут делили с новым кодом, легко было сломать)
        m2 = meters.add("m2-dev", "Прибор 2")
        s2 = sources.open_source(m2.id, "wb8-main", "m2-dev")
        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/replace-meter", json={
            "meter_source_id": s2.id, "expected_revision": rev,
        })
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["meter_source_id"] == s2.id
        print("[OK] ОТКАЗ пустого тела 400 + легитимный старый вариант "
              "(meter_source_id) по-прежнему работает на том же маршруте")
    finally:
        db.close(); os.unlink(path)


def test_new_meter_without_expected_revision_409_but_with_it_succeeds():
    """Парная проверка протокола ревизий (§6.1/§9.2) именно для нового
    пути new_meter: без expected_revision — 409, ничего не создаётся; с
    правильной ревизией — 200, и прибор/источник созданы ровно один раз."""
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p29.6", "name": "Точка 29.6"})
        point_id = r.get_json()["id"]
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        bindings = PointBindingRepo(db)
        old_meter = meters.add("old-dev-6", "Старый")
        old_source = sources.open_source(old_meter.id, "wb8-main", "old-dev-6")
        bindings.open_binding(point_id, old_source.id, "total_3p", role="primary",
                               valid_from=0)

        # ОТКАЗ: без expected_revision — 409, meter НЕ создан вообще
        r = client.post(f"/api/v2/points/{point_id}/replace-meter", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": "new-dev-6"},
        })
        assert r.status_code == 409, r.get_json()
        assert meters.get_by_device_id("new-dev-6") is None, (
            "конфликт ревизии обязан откатить ВСЮ транзакцию, включая уже "
            "созданный до отказа meter — партия 2 требует атомарности")

        # ЛЕГИТИМНЫЙ ЗАПРОС РАБОТАЕТ: с правильной ревизией — 200
        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/replace-meter", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": "new-dev-6"},
            "expected_revision": rev,
        })
        assert r.status_code == 200, r.get_json()
        assert meters.get_by_device_id("new-dev-6") is not None
        print("[OK] ОТКАЗ без expected_revision -> 409 (meter не создан, атомарно) "
              "+ легитимный запрос с ревизией -> 200")
    finally:
        db.close(); os.unlink(path)


def test_a17_a20_regression_unaffected_by_new_route_shape():
    """Явная регрессия: изменение формы маршрута replace-meter (общий
    код для двух вариантов тела) не должно менять поведение существующих
    сценариев A17-A20, покрытых test_step15_binding_service.py — здесь
    только смок-проверка через HTTP, что старый путь без new_meter
    даёт тот же результат сегментации, что и раньше."""
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p29.7", "name": "Точка 29.7"})
        point_id = r.get_json()["id"]
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        bindings = PointBindingRepo(db)
        m1 = meters.add("a20-dev-1", "Прибор 1")
        s1 = sources.open_source(m1.id, "wb8-main", "a20-dev-1")
        bindings.open_binding(point_id, s1.id, "total_3p", role="primary", valid_from=0)

        m2 = meters.add("a20-dev-2", "Прибор 2")
        s2 = sources.open_source(m2.id, "wb8-main", "a20-dev-2")
        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/replace-meter", json={
            "meter_source_id": s2.id, "at": HOUR, "expected_revision": rev,
        })
        assert r.status_code == 200, r.get_json()
        history = bindings.list_for_point(point_id)
        assert len(history) == 2
        seg1 = [b for b in history if b.meter_source_id == s1.id][0]
        seg2 = [b for b in history if b.meter_source_id == s2.id][0]
        assert seg1.valid_from == 0 and seg1.valid_to == HOUR
        assert seg2.valid_from == HOUR and seg2.valid_to is None
        print("[OK] A17-A20 регрессия: старый путь (meter_source_id) через "
              "общий маршрут сегментирует привязку так же, как раньше")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_new_meter_provisioning_creates_meter_and_source_and_replaces()
    test_new_meter_reuses_existing_meter_by_device_id()
    test_new_meter_retry_is_idempotent_no_duplicate_source()
    test_new_meter_address_taken_by_another_meter_rejected()
    test_new_meter_missing_fields_rejected_but_legacy_path_still_works()
    test_new_meter_without_expected_revision_409_but_with_it_succeeds()
    test_a17_a20_regression_unaffected_by_new_route_shape()
    print("\nВсе тесты провижининга прибора при замене (Шаг 29, партия 3, "
          "задача 3) пройдены.")

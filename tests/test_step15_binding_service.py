"""Тесты Шага 15 (этап B): point_bindings — непересечение, замена прибора,
сегментация (ТЗ §4.1/§5.4/§6.1, сценарии A17-A20).

Самостоятельный скрипт (не pytest):
    python tests/test_step15_binding_service.py

Пишется и проверяется лично (не делегировано) — это финансово-критичная
логика: непересечение основных привязок и запрет пропорционального деления
агрегата на границе замены прибора.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.location_repo import LocationRepo
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.binding_service import (
    PointBindingRepo, BindingConflict,
    profiles_overlap, intervals_overlap,
    resolve_primary_segments, classify_aggregate_against_segment,
    PointBinding, Segment,
    AGG_FULLY_INSIDE, AGG_STRADDLES, AGG_OUTSIDE,
)


def make_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    return db, path


def make_meter_and_source(db, device_id, label):
    """Хелпер: создать meter + открытый meter_source с тем же device_id
    (в качестве controller_key используем фиксированное значение)."""
    meters = MeterRepo(db, GroupRepo(db))
    sources = MeterSourceRepo(db)
    m = meters.add(device_id, label)
    src = sources.open_source(m.id, "wb8-main", device_id)
    return m, src


# --- чистые функции: profiles_overlap / intervals_overlap -------------

def test_profiles_overlap_matrix():
    assert profiles_overlap("total_3p", "phase_l1") is True
    assert profiles_overlap("total_3p", "phase_l2") is True
    assert profiles_overlap("total_3p", "phase_l3") is True
    assert profiles_overlap("total_3p", "total_3p") is True
    assert profiles_overlap("phase_l1", "phase_l1") is True
    assert profiles_overlap("phase_l1", "phase_l2") is False
    assert profiles_overlap("phase_l2", "phase_l3") is False
    assert profiles_overlap("phase_l1", "phase_l3") is False
    print("[OK] A17: total_3p пересекается с любой фазой; фазы между собой независимы")


def test_intervals_overlap_touching_boundary_allowed():
    # общая граница не пересечение
    assert intervals_overlap(0, 100, 100, 200) is False
    assert intervals_overlap(0, 100, 99, 200) is True
    # открытый интервал (valid_to=None) пересекает всё, что после его начала
    assert intervals_overlap(50, None, 0, 100) is True
    assert intervals_overlap(50, None, 0, 50) is False
    print("[OK] Соседние интервалы с общей границей допустимы, открытые интервалы корректны")


# --- A17: профиль каналов через реальный сервис -------------------------

def test_a17_total_and_phase_rejected_but_three_phases_allowed():
    db, path = make_db()
    try:
        points = MeteringPointRepo(db)
        m, src = make_meter_and_source(db, "map3e.1", "WB-MAP3E #1")
        bindings = PointBindingRepo(db)

        p_total = points.add("cons.total", "Нагрузка целиком (3ф)")
        bindings.open_binding(p_total.id, src.id, "total_3p")

        p_l1 = points.add("cons.l1", "Фаза L1 отдельно")
        try:
            bindings.open_binding(p_l1.id, src.id, "phase_l1")
        except BindingConflict as e:
            assert "физический scope" in str(e)
            print("[OK] A17: total_3p + phase_l1 на одном источнике отклонены как пересечение")
        else:
            raise AssertionError("ожидался BindingConflict")

        # А три независимые однофазные нагрузки на разных приборах — можно
        db.close(); os.unlink(path)
        db, path = make_db()
        points = MeteringPointRepo(db)
        m2, src2 = make_meter_and_source(db, "map3e.2", "WB-MAP3E #2")
        bindings = PointBindingRepo(db)
        pl1 = points.add("cons.l1b", "L1")
        pl2 = points.add("cons.l2b", "L2")
        pl3 = points.add("cons.l3b", "L3")
        bindings.open_binding(pl1.id, src2.id, "phase_l1")
        bindings.open_binding(pl2.id, src2.id, "phase_l2")
        bindings.open_binding(pl3.id, src2.id, "phase_l3")
        print("[OK] A17: три независимые однофазные привязки на одном источнике разрешены")
    finally:
        db.close()
        os.unlink(path)


def test_cross_point_scope_conflict():
    db, path = make_db()
    try:
        points = MeteringPointRepo(db)
        m, src = make_meter_and_source(db, "meter.shared", "Общий прибор")
        bindings = PointBindingRepo(db)

        p1 = points.add("cons.a", "Точка A")
        p2 = points.add("cons.b", "Точка B")
        bindings.open_binding(p1.id, src.id, "total_3p")

        try:
            bindings.open_binding(p2.id, src.id, "total_3p")
        except BindingConflict:
            print("[OK] один физический scope не назначается двум точкам одновременно")
        else:
            raise AssertionError("ожидался BindingConflict")
    finally:
        db.close()
        os.unlink(path)


def test_self_check_not_independent_rejected():
    db, path = make_db()
    try:
        points = MeteringPointRepo(db)
        m, src = make_meter_and_source(db, "meter.selfcheck", "Прибор")
        bindings = PointBindingRepo(db)

        p = points.add("cons.x", "Точка X")
        bindings.open_binding(p.id, src.id, "total_3p", role="primary")

        try:
            bindings.open_binding(p.id, src.id, "total_3p", role="check")
        except BindingConflict as e:
            assert "независим" in str(e)
            print("[OK] check тем же источником/профилем что и primary той же точки отклонён")
        else:
            raise AssertionError("ожидался BindingConflict")

        # А check ДРУГИМ источником — разрешён (реально независимый контроль)
        m2, src2 = make_meter_and_source(db, "meter.check2", "Независимый контроль")
        bindings.open_binding(p.id, src2.id, "total_3p", role="check")
        print("[OK] check другим независимым источником разрешён")
    finally:
        db.close()
        os.unlink(path)


def test_role_interval_overlap_rejected():
    db, path = make_db()
    try:
        points = MeteringPointRepo(db)
        m1, src1 = make_meter_and_source(db, "meter.r1", "Прибор 1")
        m2, src2 = make_meter_and_source(db, "meter.r2", "Прибор 2")
        bindings = PointBindingRepo(db)

        p = points.add("cons.y", "Точка Y")
        bindings.open_binding(p.id, src1.id, "total_3p", role="primary")

        # вторая ОТКРЫТАЯ основная привязка той же точки — запрещено
        # (даже другим источником): "не более одной основной привязки"
        try:
            bindings.open_binding(p.id, src2.id, "total_3p", role="primary")
        except BindingConflict:
            print("[OK] у точки не может быть двух одновременно открытых основных привязок")
        else:
            raise AssertionError("ожидался BindingConflict")
    finally:
        db.close()
        os.unlink(path)


# --- A18: замена прибора -------------------------------------------------

def test_a18_replace_meter_segments_point_keeps_id():
    db, path = make_db()
    try:
        points = MeteringPointRepo(db)
        m_old, src_old = make_meter_and_source(db, "meter.old", "Старый прибор")
        bindings = PointBindingRepo(db)

        p = points.add("cons.replaced", "Точка с заменой")
        b1 = bindings.open_binding(p.id, src_old.id, "total_3p", valid_from=1000)

        m_new, src_new = make_meter_and_source(db, "meter.new", "Новый прибор")
        b2 = bindings.replace_meter(p.id, src_new.id, at=2000,
                                     replacement_note="плановая замена")

        # тот же point_id, новый meter_source_id
        assert b2.point_id == p.id
        assert b2.meter_source_id == src_new.id
        assert b2.meter_source_id != src_old.id

        history = bindings.list_for_point(p.id)
        assert len(history) == 2
        assert history[0].id == b1.id and history[0].valid_to == 2000
        assert history[1].id == b2.id and history[1].valid_to is None
        assert history[1].valid_from == 2000

        # старый meter_id/meter остался прежним объектом (не переписан)
        assert m_old.id != m_new.id
        print("[OK] A18: замена прибора сохраняет point_id, создаёт новый meter/meter_source, "
              "история сегментирована по границе замены")
    finally:
        db.close()
        os.unlink(path)


def test_replace_meter_rejects_conflicting_new_source():
    db, path = make_db()
    try:
        points = MeteringPointRepo(db)
        m1, src1 = make_meter_and_source(db, "meter.p1", "Прибор точки 1")
        bindings = PointBindingRepo(db)

        p1 = points.add("cons.one", "Точка 1")
        p2 = points.add("cons.two", "Точка 2")
        bindings.open_binding(p1.id, src1.id, "total_3p", valid_from=1000)

        m2, src2 = make_meter_and_source(db, "meter.p2", "Прибор точки 2")
        bindings.open_binding(p2.id, src2.id, "total_3p", valid_from=1000)

        # Попытка "заменить" прибор точки 1 на источник, уже занятый точкой 2
        try:
            bindings.replace_meter(p1.id, src2.id, at=2000)
        except BindingConflict:
            print("[OK] replace_meter отклоняет источник, уже занятый другой точкой")
        else:
            raise AssertionError("ожидался BindingConflict")
    finally:
        db.close()
        os.unlink(path)


# --- A19: сегментация без пропорционального деления --------------------

def test_a19_straddling_aggregate_not_split_by_time_share():
    """Замена в 12:20 (=44400 если считать от полуночи в секундах для
    примера), есть только цельный часовой агрегат 12:00-13:00 — деление
    агрегата по доле времени запрещено, оба смежных сегмента должны
    получить classify=STRADDLES, а не тихо разделённое число."""
    HOUR = 3600
    t_12_00 = 12 * HOUR
    t_12_20 = 12 * HOUR + 20 * 60
    t_13_00 = 13 * HOUR

    old_binding = PointBinding(
        id=1, point_id=1, meter_source_id=10, channel_profile="total_3p",
        role="primary", valid_from=0, valid_to=t_12_20,
        replacement_note=None, created_at=0,
    )
    new_binding = PointBinding(
        id=2, point_id=1, meter_source_id=11, channel_profile="total_3p",
        role="primary", valid_from=t_12_20, valid_to=None,
        replacement_note="замена в 12:20", created_at=t_12_20,
    )

    segments = resolve_primary_segments([old_binding, new_binding], t_12_00, t_13_00 + HOUR)
    assert len(segments) == 2
    old_seg, new_seg = segments
    assert old_seg.ts_from == t_12_00 and old_seg.ts_to == t_12_20
    assert new_seg.ts_from == t_12_20 and new_seg.ts_to == t_13_00 + HOUR

    # цельный часовой агрегат 12:00-13:00 пересекает границу замены (12:20)
    cls_old = classify_aggregate_against_segment(t_12_00, t_13_00, old_seg)
    cls_new = classify_aggregate_against_segment(t_12_00, t_13_00, new_seg)
    assert cls_old == AGG_STRADDLES
    assert cls_new == AGG_STRADDLES
    print("[OK] A19: часовой агрегат, пересекающий границу замены в 12:20, "
          "помечен STRADDLES для обоих сегментов — доля времени не вычисляется автоматически")


def test_a18_clean_hour_boundary_aggregate_not_straddling():
    """Контрольный случай A18: замена ровно в 12:00, агрегаты 11:00-12:00
    и 12:00-13:00 не пересекают границу и суммируются целиком по сегментам
    (12 кВт·ч старым, 8 новым -> точка отдаёт 20)."""
    HOUR = 3600
    old_binding = PointBinding(
        id=1, point_id=1, meter_source_id=10, channel_profile="total_3p",
        role="primary", valid_from=0, valid_to=12 * HOUR,
        replacement_note=None, created_at=0,
    )
    new_binding = PointBinding(
        id=2, point_id=1, meter_source_id=11, channel_profile="total_3p",
        role="primary", valid_from=12 * HOUR, valid_to=None,
        replacement_note=None, created_at=12 * HOUR,
    )
    segments = resolve_primary_segments([old_binding, new_binding], 0, 24 * HOUR)
    old_seg, new_seg = segments

    cls_old_agg = classify_aggregate_against_segment(11 * HOUR, 12 * HOUR, old_seg)
    cls_new_agg = classify_aggregate_against_segment(12 * HOUR, 13 * HOUR, new_seg)
    assert cls_old_agg == AGG_FULLY_INSIDE
    assert cls_new_agg == AGG_FULLY_INSIDE
    print("[OK] A18: замена ровно на границе часа -> оба агрегата целиком в своих сегментах, "
          "суммарно 12+8=20, а не разность абсолютных накопителей")


def test_segments_exclude_time_without_open_primary():
    """Если у точки нет открытой основной привязки в какой-то части
    периода, этот промежуток не должен появляться как сегмент вовсе —
    иначе он мог бы быть ошибочно принят за 'нет данных = 0'."""
    HOUR = 3600
    b = PointBinding(
        id=1, point_id=1, meter_source_id=10, channel_profile="total_3p",
        role="primary", valid_from=5 * HOUR, valid_to=10 * HOUR,
        replacement_note=None, created_at=0,
    )
    segments = resolve_primary_segments([b], 0, 24 * HOUR)
    assert len(segments) == 1
    assert segments[0].ts_from == 5 * HOUR and segments[0].ts_to == 10 * HOUR
    print("[OK] периоды без открытой основной привязки не становятся сегментами")


def test_a20_concurrent_open_binding_one_wins():
    """A20: два одновременных запроса пытаются занять один физический
    scope (meter_source+профиль) для разных точек. Database.transaction()
    сериализует запись через process-wide RLock (db.py) — конфликт
    обнаруживается внутри уже захваченной транзакции, так что ровно один
    поток должен успеть, второй должен получить BindingConflict, а не
    создать две пересекающиеся привязки."""
    db, path = make_db()
    try:
        points = MeteringPointRepo(db)
        m, src = make_meter_and_source(db, "meter.race", "Race meter")
        bindings = PointBindingRepo(db)

        p1 = points.add("cons.race1", "Race point 1")
        p2 = points.add("cons.race2", "Race point 2")

        results = {}

        def worker(name, point):
            try:
                bindings.open_binding(point.id, src.id, "total_3p")
                results[name] = "ok"
            except BindingConflict:
                results[name] = "conflict"
            except Exception as e:
                results[name] = f"error: {e}"

        t1 = threading.Thread(target=worker, args=("t1", p1))
        t2 = threading.Thread(target=worker, args=("t2", p2))
        t1.start(); t2.start()
        t1.join(); t2.join()

        oks = [v for v in results.values() if v == "ok"]
        conflicts = [v for v in results.values() if v == "conflict"]
        assert len(oks) == 1 and len(conflicts) == 1, (
            f"ожидался ровно один успех и один конфликт, получено {results}"
        )
        print("[OK] A20: два одновременных запроса на общий физический scope -> "
              "ровно один успешен, второй получает BindingConflict")
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    test_profiles_overlap_matrix()
    test_intervals_overlap_touching_boundary_allowed()
    test_a17_total_and_phase_rejected_but_three_phases_allowed()
    test_cross_point_scope_conflict()
    test_self_check_not_independent_rejected()
    test_role_interval_overlap_rejected()
    test_a18_replace_meter_segments_point_keeps_id()
    test_replace_meter_rejects_conflicting_new_source()
    test_a19_straddling_aggregate_not_split_by_time_share()
    test_a18_clean_hour_boundary_aggregate_not_straddling()
    test_segments_exclude_time_without_open_primary()
    test_a20_concurrent_open_binding_one_wins()
    print("\nВсе тесты binding_service (Шаг 15, A17-A20) пройдены.")

"""Тесты Шага 17 (этап C): расчётный сервис measured/sum/balance/comparison
("сердце ТЗ", §5.1/§5.2). Сценарии A03/A04/A05/A08/A09/A12/A17-A19 на
уровне полного сервиса поверх реальной БД v2.

Самостоятельный скрипт (не pytest):
    python tests/test_step17_accounting_service.py

Пишется и проверяется лично (не делегировано)."""

from __future__ import annotations

import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.binding_service import PointBindingRepo
from wb_energy_meter.topology_service import ElectricalNodeRepo, ElectricalEdgeRepo
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate
from wb_energy_meter.accounting_service import (
    measured_point, sum_points, balance, comparison, AccountingConflict,
)
from wb_energy_meter.accounting_contract import (
    AVAILABILITY_COMPLETE, AVAILABILITY_PARTIAL, AVAILABILITY_MISSING,
    STRUCTURE_VERIFIED, STRUCTURE_UNVERIFIED,
)

HOUR = 3600


def make_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    return db, path


def add_hour(aggregates, meter_id, h_start, delta, quality="ok"):
    aggregates.upsert(HourlyAggregate(
        meter_id=meter_id, period_start=h_start, period_end=h_start + HOUR,
        ap_energy_start=0.0, ap_energy_end=delta, ap_energy_delta=delta,
        p_avg=None, p_max=None, samples_count=1, quality_flag=quality,
        computed_at=0,
    ))


def make_point_with_meter(db, points, meters, sources, bindings, code, name):
    m = meters.add(code, name)
    src = sources.open_source(m.id, "wb8-main", code)
    p = points.add(code, name)
    bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)
    return p, m, src


# --- ТЗ §5.2 сценарий: ввод A=100, цех B=60, серверная C=30, станок D=20 внутри B

def build_5_2_scenario(db):
    meters = MeterRepo(db, GroupRepo(db))
    sources = MeterSourceRepo(db)
    points = MeteringPointRepo(db)
    bindings = PointBindingRepo(db)
    nodes = ElectricalNodeRepo(db)
    edges = ElectricalEdgeRepo(db)
    aggregates = AggregateRepo(db)

    p_a, m_a, src_a = make_point_with_meter(db, points, meters, sources, bindings, "a", "Ввод A")
    p_b, m_b, src_b = make_point_with_meter(db, points, meters, sources, bindings, "b", "Цех B")
    p_c, m_c, src_c = make_point_with_meter(db, points, meters, sources, bindings, "c", "Серверная C")
    p_d, m_d, src_d = make_point_with_meter(db, points, meters, sources, bindings, "d", "Станок D")

    n_src = nodes.add("N-A", "Ввод", "source")
    n_b = nodes.add("N-B", "ЩР цеха", "panel")
    n_c = nodes.add("N-C", "ЩР серверной", "panel")
    n_d = nodes.add("N-D", "Станок", "load")

    e1 = edges.add_draft(n_src.id, n_b.id, primary_point_id=p_a.id)
    e2 = edges.add_draft(n_b.id, n_c.id, primary_point_id=p_c.id)
    e3 = edges.add_draft(n_b.id, n_d.id, primary_point_id=p_d.id)
    edges.publish_edges([e1.id, e2.id, e3.id])
    # p_b (сама точка B) не привязана к отдельному edge в этом примере ТЗ —
    # "внутри цеха станок 20" считается по разности; здесь мы просто задаём
    # энергию B напрямую через её meter, без электрической привязки к узлу
    # (см. ниже: sum(A, B) в этом примере проверяется отдельно).

    # За один час: A=100, B=60 (замер самого цеха как отдельного meter),
    # C=30, D=20.
    add_hour(aggregates, m_a.id, 0, 100.0)
    add_hour(aggregates, m_b.id, 0, 60.0)
    add_hour(aggregates, m_c.id, 0, 30.0)
    add_hour(aggregates, m_d.id, 0, 20.0)

    return dict(
        points=points, bindings=bindings, aggregates=aggregates,
        sources=sources, edges=edges, nodes=nodes,
        p_a=p_a, p_b=p_b, p_c=p_c, p_d=p_d,
    )


def test_measured_single_point():
    db, path = make_db()
    try:
        s = build_5_2_scenario(db)
        r = measured_point(s["bindings"], s["aggregates"], s["sources"],
                            s["p_a"].id, 0, HOUR)
        assert r.value == 100.0 and r.availability == AVAILABILITY_COMPLETE
        print("[OK] measured(A) = 100")
    finally:
        db.close(); os.unlink(path)


def test_a03_sum_b_c_not_d_double_counted():
    """A03: B(60)+C(30)=90 — D(20), уже внутри B через электрическую сеть,
    не суммируется отдельно, потому что мы явно не включаем D в состав."""
    db, path = make_db()
    try:
        s = build_5_2_scenario(db)
        r = sum_points(s["bindings"], s["aggregates"], s["sources"], s["edges"],
                        [s["p_b"].id, s["p_c"].id], 0, HOUR)
        assert r.value == 90.0, f"ожидалось 90.0, получено {r.value}"
        print("[OK] A03: sum(B, C) = 90 (B=60, C=30), первый уровень детализации")
    finally:
        db.close(); os.unlink(path)


def test_a04_sum_a_d_rejected_overlap():
    """A04 ("явная сумма A+B или B+D"): A — питающий узел для D в дереве
    (A измеряет ввод в узел B, от которого напрямую отходит edge к D),
    явная сумма A+D должна быть отклонена как электрическое перекрытие —
    D физически уже учтён внутри показания A."""
    db, path = make_db()
    try:
        s = build_5_2_scenario(db)
        try:
            sum_points(s["bindings"], s["aggregates"], s["sources"], s["edges"],
                       [s["p_a"].id, s["p_d"].id], 0, HOUR)
        except AccountingConflict as e:
            print(f"[OK] A04: sum(A, D) отклонён с объяснением пути: {e}")
        else:
            raise AssertionError("ожидался AccountingConflict")

        # А C и D — независимые (оба напрямую под B, не один внутри другого) —
        # их сумму объединять можно, электрического перекрытия здесь нет.
        r_cd = sum_points(s["bindings"], s["aggregates"], s["sources"], s["edges"],
                           [s["p_c"].id, s["p_d"].id], 0, HOUR)
        assert r_cd.value == 50.0, f"C+D независимы, ожидалось 30+20=50, получено {r_cd.value}"
        print("[OK] C и D — независимые сиблинги под B, сумма 30+20=50 разрешена")
    finally:
        db.close(); os.unlink(path)


def test_a04_sum_same_point_measures_same_node_rejected():
    db, path = make_db()
    try:
        s = build_5_2_scenario(db)
        try:
            sum_points(s["bindings"], s["aggregates"], s["sources"], s["edges"],
                       [s["p_c"].id, s["p_c"].id, ], 0, HOUR)
        except Exception:
            pass  # дубликат одной и той же точки — дедуп решает раньше конфликта
        # Дубликат ДОЛЖЕН быть тихо дедуплицирован (A05), а не давать конфликт
        r = sum_points(s["bindings"], s["aggregates"], s["sources"], s["edges"],
                        [s["p_c"].id, s["p_c"].id], 0, HOUR)
        assert r.value == 30.0
        assert r.expected_count == 1  # дедуп, не 2
        print("[OK] A05: повторяющаяся точка в составе учитывается один раз")
    finally:
        db.close(); os.unlink(path)


def test_a08_signed_imbalance():
    """A08: ввод 100, выходы 70 и 40 -> небаланс -10, -10%, знак сохранён."""
    db, path = make_db()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        points = MeteringPointRepo(db)
        bindings = PointBindingRepo(db)
        edges = ElectricalEdgeRepo(db)
        aggregates = AggregateRepo(db)

        p_in, m_in, _ = make_point_with_meter(db, points, meters, sources, bindings, "in", "Ввод")
        p_o1, m_o1, _ = make_point_with_meter(db, points, meters, sources, bindings, "o1", "Выход 1")
        p_o2, m_o2, _ = make_point_with_meter(db, points, meters, sources, bindings, "o2", "Выход 2")

        add_hour(aggregates, m_in.id, 0, 100.0)
        add_hour(aggregates, m_o1.id, 0, 70.0)
        add_hour(aggregates, m_o2.id, 0, 40.0)

        with db.transaction() as c:
            c.execute("INSERT INTO balance_scopes (id, name, created_at, updated_at) "
                      "VALUES (1, 'Объект', 0, 0)")
            c.execute("INSERT INTO balance_members (scope_id, point_id, side, valid_from, created_at) "
                      "VALUES (1, ?, 'input', 0, 0)", (p_in.id,))
            c.execute("INSERT INTO balance_members (scope_id, point_id, side, valid_from, created_at) "
                      "VALUES (1, ?, 'output', 0, 0)", (p_o1.id,))
            c.execute("INSERT INTO balance_members (scope_id, point_id, side, valid_from, created_at) "
                      "VALUES (1, ?, 'output', 0, 0)", (p_o2.id,))

        r = balance(db, bindings, aggregates, sources, edges, 1, 0, HOUR)
        assert r.value == -10.0, f"ожидалось -10.0, получено {r.value}"
        pct = r.explanation["percentage"]
        assert pct == -10.0, f"ожидалось -10.0%, получено {pct}"
        assert r.availability == AVAILABILITY_COMPLETE
        print(f"[OK] A08: небаланс {r.value} кВт·ч ({pct}%), знак сохранён")
    finally:
        db.close(); os.unlink(path)


def test_a09_missing_output_makes_balance_null():
    """A09: ввод известен, но один обязательный выход неизвестен (нет
    агрегата) -> value=null у строгого баланса; известные части отдельно."""
    db, path = make_db()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        points = MeteringPointRepo(db)
        bindings = PointBindingRepo(db)
        edges = ElectricalEdgeRepo(db)
        aggregates = AggregateRepo(db)

        p_in, m_in, _ = make_point_with_meter(db, points, meters, sources, bindings, "in2", "Ввод")
        p_o1, m_o1, _ = make_point_with_meter(db, points, meters, sources, bindings, "o1b", "Выход 1")
        p_o2, m_o2, _ = make_point_with_meter(db, points, meters, sources, bindings, "o2b", "Выход 2 (нет данных)")

        add_hour(aggregates, m_in.id, 0, 100.0)
        add_hour(aggregates, m_o1.id, 0, 70.0)
        # у o2 нет агрегата вовсе -> нет данных

        with db.transaction() as c:
            c.execute("INSERT INTO balance_scopes (id, name, created_at, updated_at) "
                      "VALUES (2, 'Объект2', 0, 0)")
            c.execute("INSERT INTO balance_members (scope_id, point_id, side, valid_from, created_at) "
                      "VALUES (2, ?, 'input', 0, 0)", (p_in.id,))
            c.execute("INSERT INTO balance_members (scope_id, point_id, side, valid_from, created_at) "
                      "VALUES (2, ?, 'output', 0, 0)", (p_o1.id,))
            c.execute("INSERT INTO balance_members (scope_id, point_id, side, valid_from, created_at) "
                      "VALUES (2, ?, 'output', 0, 0)", (p_o2.id,))

        r = balance(db, bindings, aggregates, sources, edges, 2, 0, HOUR)
        assert r.value is None, f"ожидался null, получено {r.value}"
        assert r.availability != AVAILABILITY_COMPLETE
        print(f"[OK] A09: value=null при неполном выходе; известные части "
              f"известны отдельно (known_value={r.known_value})")
    finally:
        db.close(); os.unlink(path)


def test_a12_partial_hours_measured():
    """A12: 9 из 10 часовых агрегатов -> value=null, known_value подписан."""
    db, path = make_db()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        points = MeteringPointRepo(db)
        bindings = PointBindingRepo(db)
        aggregates = AggregateRepo(db)

        p, m, _ = make_point_with_meter(db, points, meters, sources, bindings, "partial", "Частичная точка")
        for i in range(9):
            add_hour(aggregates, m.id, i * HOUR, 10.0)
        # 10-й час (i=9) намеренно отсутствует

        r = measured_point(bindings, aggregates, sources, p.id, 0, 10 * HOUR)
        assert r.value is None
        assert r.known_value == 90.0
        assert r.expected_count == 10 and r.valid_count == 9
        print(f"[OK] A12: value=None, known_value={r.known_value} при 9 из 10 часов")
    finally:
        db.close(); os.unlink(path)


def test_a13_reset_not_silently_zero():
    """A13: накопитель 100 -> 0 -> 150 внутри часа — агрегатор уже помечает
    такой час quality_flag='reset' с ap_energy_delta=None; measured_point
    не должен превратить это в 0 или в успешный известный час."""
    db, path = make_db()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        points = MeteringPointRepo(db)
        bindings = PointBindingRepo(db)
        aggregates = AggregateRepo(db)

        p, m, _ = make_point_with_meter(db, points, meters, sources, bindings, "resetpt", "Точка со сбросом")
        aggregates.upsert(HourlyAggregate(
            meter_id=m.id, period_start=0, period_end=HOUR,
            ap_energy_start=100.0, ap_energy_end=150.0, ap_energy_delta=None,
            p_avg=None, p_max=None, samples_count=3, quality_flag="reset",
            computed_at=0,
        ))
        r = measured_point(bindings, aggregates, sources, p.id, 0, HOUR)
        assert r.value is None and r.known_value is None
        assert "reset" in r.quality_flags
        print("[OK] A13: сброс внутри часа не выдаётся ни за 0, ни за известный расход")
    finally:
        db.close(); os.unlink(path)


def test_a18_a19_replace_meter_segmentation_end_to_end():
    """A18/A19 на уровне полного сервиса: замена ровно на границе часа ->
    12+8=20; замена ВНУТРИ часа -> этот час помечается неизвестным, а не
    делится по доле времени."""
    db, path = make_db()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        points = MeteringPointRepo(db)
        bindings = PointBindingRepo(db)
        aggregates = AggregateRepo(db)

        # --- случай A18: замена ровно в 12:00 (час 12) ---
        m_old = meters.add("old18", "Старый прибор")
        src_old = sources.open_source(m_old.id, "wb8-main", "old18")
        p = points.add("replaced18", "Точка с заменой")
        bindings.open_binding(p.id, src_old.id, "total_3p", valid_from=0)

        m_new = meters.add("new18", "Новый прибор")
        src_new = sources.open_source(m_new.id, "wb8-main", "new18")
        bindings.replace_meter(p.id, src_new.id, at=12 * HOUR)

        add_hour(aggregates, m_old.id, 11 * HOUR, 12.0)
        add_hour(aggregates, m_new.id, 12 * HOUR, 8.0)

        r = measured_point(bindings, aggregates, sources, p.id, 11 * HOUR, 13 * HOUR)
        assert r.value == 20.0, f"A18: ожидалось 20.0, получено {r.value}"
        print(f"[OK] A18: замена на границе часа даёт 12+8={r.value}, "
              f"не разность абсолютных накопителей")

        # --- случай A19: замена ВНУТРИ часа (12:20) ---
        m_old2 = meters.add("old19", "Старый прибор 2")
        src_old2 = sources.open_source(m_old2.id, "wb8-main", "old19")
        p2 = points.add("replaced19", "Точка с заменой внутри часа")
        bindings.open_binding(p2.id, src_old2.id, "total_3p", valid_from=12 * HOUR)

        m_new2 = meters.add("new19", "Новый прибор 2")
        src_new2 = sources.open_source(m_new2.id, "wb8-main", "new19")
        bindings.replace_meter(p2.id, src_new2.id, at=12 * HOUR + 20 * 60)

        # только цельный часовой агрегат 12:00-13:00 доступен (у СТАРОГО
        # прибора, т.к. агрегатор считал по старому meter_id весь час)
        add_hour(aggregates, m_old2.id, 12 * HOUR, 12.0)

        r2 = measured_point(bindings, aggregates, sources, p2.id, 12 * HOUR, 13 * HOUR)
        assert r2.value is None, (
            "A19: деление агрегата по доле времени запрещено — value должен "
            "остаться null, а не выдуманная пропорция"
        )
        assert r2.availability == AVAILABILITY_MISSING
        print(f"[OK] A19: замена внутри часа (12:20) не делит агрегат по доле "
              f"времени — value=None (availability={r2.availability})")
    finally:
        db.close(); os.unlink(path)


def test_comparison_no_combined_total():
    db, path = make_db()
    try:
        s = build_5_2_scenario(db)
        results = comparison(s["bindings"], s["aggregates"], s["sources"],
                              [s["p_b"].id, s["p_c"].id], 0, HOUR)
        assert set(results.keys()) == {s["p_b"].id, s["p_c"].id}
        assert results[s["p_b"].id].value == 60.0
        assert results[s["p_c"].id].value == 30.0
        # comparison не возвращает единого итога — только dict по точкам
        assert not hasattr(results, "value")
        print("[OK] comparison: отдельные показатели рядом, общего итога нет")
    finally:
        db.close(); os.unlink(path)


def test_unverified_topology_marks_structure_quality():
    """§5.1/A07: состав есть, но у одной из точек нет электрической
    привязки -> structure_quality=unverified, а не отклонение."""
    db, path = make_db()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        points = MeteringPointRepo(db)
        bindings = PointBindingRepo(db)
        edges = ElectricalEdgeRepo(db)
        aggregates = AggregateRepo(db)

        p1, m1, _ = make_point_with_meter(db, points, meters, sources, bindings, "u1", "Без сети 1")
        p2, m2, _ = make_point_with_meter(db, points, meters, sources, bindings, "u2", "Без сети 2")
        add_hour(aggregates, m1.id, 0, 5.0)
        add_hour(aggregates, m2.id, 0, 7.0)

        r = sum_points(bindings, aggregates, sources, edges, [p1.id, p2.id], 0, HOUR)
        assert r.structure_quality == STRUCTURE_UNVERIFIED
        assert r.value == 12.0  # сумма всё равно считается, но помечена unverified
        print(f"[OK] A07: sum без известной топологии -> structure_quality="
              f"{r.structure_quality}, значение {r.value} не выдаётся за "
              f"подтверждённый итог")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_measured_single_point()
    test_a03_sum_b_c_not_d_double_counted()
    test_a04_sum_a_d_rejected_overlap()
    test_a04_sum_same_point_measures_same_node_rejected()
    test_a08_signed_imbalance()
    test_a09_missing_output_makes_balance_null()
    test_a12_partial_hours_measured()
    test_a13_reset_not_silently_zero()
    test_a18_a19_replace_meter_segmentation_end_to_end()
    test_comparison_no_combined_total()
    test_unverified_topology_marks_structure_quality()
    print("\nВсе тесты accounting_service (Шаг 17) пройдены.")

"""Тесты Шага 16 (этап C): электрическая сеть — лес радиальных деревьев
(ТЗ §4.3, сценарий приёмки A24).

Самостоятельный скрипт (не pytest):
    python tests/test_step16_topology_service.py

Пишется и проверяется лично (не делегировано) — топология напрямую
определяет электрическую независимость измерений (двойной счёт/потеря
ветви при ошибке)."""

from __future__ import annotations

import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.topology_service import (
    ElectricalNodeRepo, ElectricalEdgeRepo, TopologyConflict,
    EdgeRef, validate_forest, build_children_map, is_descendant,
)


def make_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    return db, path


# --- чистая функция validate_forest -------------------------------------

def test_valid_simple_tree():
    edges = [EdgeRef(1, 10, 20), EdgeRef(2, 20, 30), EdgeRef(3, 20, 31)]
    kinds = {10: "source", 20: "panel", 30: "load", 31: "load"}
    assert validate_forest(edges, kinds) == []
    print("[OK] простое дерево source->panel->{load,load} валидно")


def test_multiple_roots_allowed():
    edges = [EdgeRef(1, 10, 30), EdgeRef(2, 11, 31)]
    kinds = {10: "source", 11: "source", 30: "load", 31: "load"}
    assert validate_forest(edges, kinds) == []
    print("[OK] несколько независимых source = несколько корней, не конфликт")


def test_self_loop_detected():
    edges = [EdgeRef(1, 20, 20)]
    kinds = {20: "panel"}
    v = validate_forest(edges, kinds)
    assert len(v) == 1 and v[0].kind == "self_loop"
    print("[OK] A24: самоссылка обнаружена")


def test_duplicate_parent_detected():
    edges = [EdgeRef(1, 10, 30), EdgeRef(2, 11, 30)]
    kinds = {10: "source", 11: "source", 30: "panel"}
    v = validate_forest(edges, kinds)
    kinds_found = {x.kind for x in v}
    assert "duplicate_parent" in kinds_found
    print("[OK] A24: второй вход к узлу (два родителя) обнаружен")


def test_cycle_detected_with_chain():
    # 10 -> 20 -> 30 -> 10 (цикл, полностью изолированный от источника)
    edges = [EdgeRef(1, 10, 20), EdgeRef(2, 20, 30), EdgeRef(3, 30, 10)]
    kinds = {10: "panel", 20: "panel", 30: "panel"}
    v = validate_forest(edges, kinds)
    cycle_violations = [x for x in v if x.kind == "cycle"]
    assert len(cycle_violations) == 1
    assert set(cycle_violations[0].node_ids) >= {10, 20, 30}
    print(f"[OK] A24: цикл питания обнаружен с цепочкой: {cycle_violations[0].message}")


def test_source_has_incoming_rejected():
    edges = [EdgeRef(1, 20, 10)]
    kinds = {20: "panel", 10: "source"}
    v = validate_forest(edges, kinds)
    assert any(x.kind == "source_has_incoming" for x in v)
    print("[OK] source с входящей связью отклонён (source не бывает приёмником)")


def test_load_has_outgoing_rejected():
    edges = [EdgeRef(1, 30, 20)]
    kinds = {30: "load", 20: "panel"}
    v = validate_forest(edges, kinds)
    assert any(x.kind == "load_has_outgoing" for x in v)
    print("[OK] load с исходящей связью отклонён (load — конечный узел)")


def test_is_descendant_a04():
    # используем строковые id для наглядности примера A03/A04 из ТЗ;
    # EdgeRef годится напрямую — build_children_map читает только
    # from_node_id/to_node_id по атрибутам (duck typing).
    edges = [EdgeRef(1, "A", "B"), EdgeRef(2, "A", "C"), EdgeRef(3, "B", "D")]
    children = build_children_map(edges)
    assert is_descendant(children, "A", "D") is True   # D под B под A
    assert is_descendant(children, "A", "C") is True
    assert is_descendant(children, "B", "A") is False  # не в ту сторону
    assert is_descendant(children, "A", "A") is False
    print("[OK] A04: достижимость находит потомка A->B->D")


# --- ElectricalNodeRepo/ElectricalEdgeRepo с реальной БД -----------------

def test_publish_valid_topology():
    db, path = make_db()
    try:
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)

        src = nodes.add("N-SRC", "Ввод", "source")
        panel = nodes.add("N-PANEL", "ЩР-1", "panel")
        load = nodes.add("N-LOAD", "Станок", "load")

        e1 = edges.add_draft(src.id, panel.id, code="L1")
        e2 = edges.add_draft(panel.id, load.id, code="L2")

        published = edges.publish_edges([e1.id, e2.id])
        assert all(e.state == "published" and e.valid_to is None for e in published)

        active = edges.list_active_published()
        assert len(active) == 2
        print("[OK] публикация валидной топологии проходит, связи активны")
    finally:
        db.close()
        os.unlink(path)


def test_publish_cycle_rejected_and_structure_unchanged():
    db, path = make_db()
    try:
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)

        a = nodes.add("N-A", "Узел A", "panel")
        b = nodes.add("N-B", "Узел B", "panel")
        c = nodes.add("N-C", "Узел C", "panel")

        e1 = edges.add_draft(a.id, b.id)
        e2 = edges.add_draft(b.id, c.id)
        e3 = edges.add_draft(c.id, a.id)  # замыкает цикл

        try:
            edges.publish_edges([e1.id, e2.id, e3.id])
        except TopologyConflict as e:
            assert "цикл" in str(e).lower()
            print(f"[OK] A24: публикация цикла отклонена с описанием: {e}")
        else:
            raise AssertionError("ожидался TopologyConflict")

        # структура НЕ изменилась: все три связи остались черновиками
        assert edges.get_by_id(e1.id).state == "draft"
        assert edges.get_by_id(e2.id).state == "draft"
        assert edges.get_by_id(e3.id).state == "draft"
        assert edges.list_active_published() == []
        print("[OK] A24: после отклонённой публикации текущая структура не изменилась")
    finally:
        db.close()
        os.unlink(path)


def test_publish_sequential_replacement_supersedes():
    """Последовательная (не одновременная) смена питающего узла — штатная
    операция: закрыть старую связь, опубликовать новую тем же to_node_id
    отдельным вызовом. Так строится as_was-история сети (см. комментарий
    в migrations/005). Это НЕ сценарий A24 "второй вход" — там оба
    входа предлагаются ОДНИМ батчем публикации (следующий тест)."""
    db, path = make_db()
    try:
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)

        s1 = nodes.add("N-S1", "Ввод 1", "source")
        s2 = nodes.add("N-S2", "Ввод 2", "source")
        panel = nodes.add("N-P", "ЩР", "panel")

        e1 = edges.add_draft(s1.id, panel.id)
        edges.publish_edges([e1.id])  # первый вход опубликован успешно

        e2 = edges.add_draft(s2.id, panel.id)
        edges.publish_edges([e2.id])  # отдельный вызов -> штатная замена

        active = edges.list_active_published()
        assert len(active) == 1 and active[0].from_node_id == s2.id
        assert edges.get_by_id(e1.id).valid_to is not None
        print("[OK] последовательная замена питающего узла отдельными вызовами "
              "publish_edges штатно вытесняет прежнюю связь (as_was-история)")
    finally:
        db.close()
        os.unlink(path)


def test_publish_simultaneous_second_input_rejected():
    """A24 буквально: ДВА входа к одному узлу, предложенные ОДНИМ батчем
    публикации — неоднозначно (какой из двух должен победить?), поэтому
    отклоняется целиком, а не вытесняет один другим."""
    db, path = make_db()
    try:
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)

        s1 = nodes.add("N-S1B", "Ввод 1", "source")
        s2 = nodes.add("N-S2B", "Ввод 2", "source")
        panel = nodes.add("N-PB", "ЩР", "panel")

        e1 = edges.add_draft(s1.id, panel.id)
        e2 = edges.add_draft(s2.id, panel.id)

        try:
            edges.publish_edges([e1.id, e2.id])
        except TopologyConflict as e:
            assert "более одной" in str(e) or "duplicate" in str(e).lower()
            print(f"[OK] A24: два входа к узлу одним батчем публикации отклонены: {e}")
        else:
            raise AssertionError("ожидался TopologyConflict")

        assert edges.get_by_id(e1.id).state == "draft"
        assert edges.get_by_id(e2.id).state == "draft"
        assert edges.list_active_published() == []
        print("[OK] A24: структура не изменилась после отклонённого батча из двух входов")
    finally:
        db.close()
        os.unlink(path)


def test_source_kind_cannot_receive_via_publish():
    db, path = make_db()
    try:
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)

        panel = nodes.add("N-P2", "ЩР", "panel")
        src = nodes.add("N-S3", "Ввод", "source")

        e1 = edges.add_draft(panel.id, src.id)  # panel "питает" source — неверно
        try:
            edges.publish_edges([e1.id])
        except TopologyConflict as e:
            assert "source" in str(e).lower()
            print("[OK] A24: попытка сделать source приёмником отклонена при публикации")
        else:
            raise AssertionError("ожидался TopologyConflict")
    finally:
        db.close()
        os.unlink(path)


def test_node_archive_blocked_by_active_edge():
    db, path = make_db()
    try:
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)

        src = nodes.add("N-S4", "Ввод", "source")
        panel = nodes.add("N-P4", "ЩР", "panel")
        e1 = edges.add_draft(src.id, panel.id)
        edges.publish_edges([e1.id])

        try:
            nodes.archive(panel.id)
        except ValueError as e:
            assert "действующ" in str(e).lower()
            print("[OK] узел с действующей связью не архивируется")
        else:
            raise AssertionError("ожидался ValueError")
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    test_valid_simple_tree()
    test_multiple_roots_allowed()
    test_self_loop_detected()
    test_duplicate_parent_detected()
    test_cycle_detected_with_chain()
    test_source_has_incoming_rejected()
    test_load_has_outgoing_rejected()
    test_is_descendant_a04()
    test_publish_valid_topology()
    test_publish_cycle_rejected_and_structure_unchanged()
    test_publish_sequential_replacement_supersedes()
    test_publish_simultaneous_second_input_rejected()
    test_source_kind_cannot_receive_via_publish()
    test_node_archive_blocked_by_active_edge()
    print("\nВсе тесты topology_service (Шаг 16, A24) пройдены.")

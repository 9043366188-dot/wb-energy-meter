"""Тесты Шага 24 (этап E/F): GroupRepoV2 — версионируемая иерархия и
версионируемый состав учётных групп v2 (ТЗ §4.4).

Самостоятельный скрипт (не pytest):
    python tests/test_step24_group_repo_v2.py

Покрывает: создание группы с кэшем parent_id + первой записью в
group_parent_bindings, проверку области видимости категории при
назначении родителя, защиту от циклов (та же техника, что и у
LocationRepo), path_to_root, корректность list_children для легаси-групп
без единой строки в group_parent_bindings, версионированное членство точек
(включая разрешённую многогруппность одной точки — ТЗ §4.4), и самое
ответственное — resolve_effective_members: дедупликация точки, попавшей в
эффективный состав несколькими путями, с сохранением происхождения (via)."""

from __future__ import annotations

import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, KvRepo
from wb_energy_meter.point_repo import MeteringPointRepo
from wb_energy_meter.group_repo_v2 import GroupRepoV2
from wb_energy_meter import domain_generation


def make_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    return db, path


def test_add_root_group_no_parent():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        g = groups.add("Арендаторы", category="tenant")
        assert g.id is not None
        assert g.name == "Арендаторы"
        assert g.category == "tenant"
        assert g.parent_id is None
        assert g.color  # авто-назначен из палитры
        print("[OK] test_add_root_group_no_parent")
    finally:
        db.close(); os.unlink(path)


def test_add_child_group_sets_parent_and_binding():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        root = groups.add("Здание", category="production")
        child = groups.add("Цех 1", category="production", parent_id=root.id)
        assert child.parent_id == root.id

        # кэш meter_groups.parent_id и версия group_parent_bindings должны
        # совпадать
        with db.read() as c:
            row = c.execute(
                "SELECT parent_id FROM group_parent_bindings "
                "WHERE group_id = ? AND valid_to IS NULL", (child.id,)
            ).fetchone()
        assert row["parent_id"] == root.id
        print("[OK] test_add_child_group_sets_parent_and_binding")
    finally:
        db.close(); os.unlink(path)


def test_add_duplicate_name_raises():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        groups.add("Склад")
        try:
            groups.add("склад")  # казефолд-конфликт, как у легаси GroupRepo
            assert False, "ожидали ValueError"
        except ValueError as e:
            assert "уже существует" in str(e)
        print("[OK] test_add_duplicate_name_raises")
    finally:
        db.close(); os.unlink(path)


def test_add_missing_parent_raises():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        try:
            groups.add("Сирота", parent_id=99999)
            assert False, "ожидали ValueError"
        except ValueError as e:
            assert "не найдена" in str(e)
        print("[OK] test_add_missing_parent_raises")
    finally:
        db.close(); os.unlink(path)


def test_category_scope_blocks_cross_category_parent():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        tenants_root = groups.add("Арендаторы", category="tenant")
        try:
            groups.add("Цех А", category="production", parent_id=tenants_root.id)
            assert False, "ожидали ValueError (разные категории)"
        except ValueError as e:
            assert "категори" in str(e).lower()
        print("[OK] test_category_scope_blocks_cross_category_parent")
    finally:
        db.close(); os.unlink(path)


def test_category_scope_allows_when_either_category_none():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        # родитель без категории -> ребёнок с категорией допустим
        root = groups.add("Без категории")
        child = groups.add("С категорией", category="production", parent_id=root.id)
        assert child.parent_id == root.id

        # родитель с категорией -> ребёнок без категории тоже допустим
        root2 = groups.add("С категорией 2", category="tenant")
        child2 = groups.add("Ещё без категории", parent_id=root2.id)
        assert child2.parent_id == root2.id
        print("[OK] test_category_scope_allows_when_either_category_none")
    finally:
        db.close(); os.unlink(path)


def test_set_parent_updates_cache_and_history():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        a = groups.add("A")
        b = groups.add("B")
        c_grp = groups.add("C", parent_id=a.id)

        moved = groups.set_parent(c_grp.id, b.id)
        assert moved.parent_id == b.id

        with db.read() as c:
            rows = c.execute(
                "SELECT parent_id, valid_to FROM group_parent_bindings "
                "WHERE group_id = ? ORDER BY id", (c_grp.id,)
            ).fetchall()
        assert len(rows) == 2
        assert rows[0]["parent_id"] == a.id and rows[0]["valid_to"] is not None
        assert rows[1]["parent_id"] == b.id and rows[1]["valid_to"] is None
        print("[OK] test_set_parent_updates_cache_and_history")
    finally:
        db.close(); os.unlink(path)


def test_set_parent_self_parent_cycle():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        a = groups.add("A")
        try:
            groups.set_parent(a.id, a.id)
            assert False, "ожидали ValueError (сам себе родитель)"
        except ValueError as e:
            assert "цикл" in str(e).lower()
        print("[OK] test_set_parent_self_parent_cycle")
    finally:
        db.close(); os.unlink(path)


def test_set_parent_deep_cycle_detection():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        a = groups.add("A")
        b = groups.add("B", parent_id=a.id)
        c_grp = groups.add("C", parent_id=b.id)
        # попытка сделать A ребёнком C (C -> B -> A -> C) -- цикл
        try:
            groups.set_parent(a.id, c_grp.id)
            assert False, "ожидали ValueError (цикл через 3 узла)"
        except ValueError as e:
            assert "цикл" in str(e).lower()
        # исходная иерархия не должна была измениться
        assert groups.get_by_id(a.id).parent_id is None
        print("[OK] test_set_parent_deep_cycle_detection")
    finally:
        db.close(); os.unlink(path)


def test_path_to_root():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        a = groups.add("Объект")
        b = groups.add("Корпус", parent_id=a.id)
        c_grp = groups.add("Цех", parent_id=b.id)

        path_ = groups.path_to_root(c_grp.id)
        assert [g.name for g in path_] == ["Объект", "Корпус", "Цех"]
        print("[OK] test_path_to_root")
    finally:
        db.close(); os.unlink(path)


def test_list_children_includes_legacy_groups_without_binding_row():
    """Группа, созданная легаси GroupRepo.create() (никогда не проходила
    через GroupRepoV2), не имеет ни одной строки в group_parent_bindings —
    но должна корректно считаться корневой через кэш parent_id (NULL по
    умолчанию и там, и там)."""
    db, path = make_db()
    try:
        legacy_groups = GroupRepo(db)
        legacy = legacy_groups.create("Легаси-зона")

        groups_v2 = GroupRepoV2(db)
        v2_root = groups_v2.add("V2-корень")

        with db.read() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM group_parent_bindings WHERE group_id = ?",
                (legacy.id,)
            ).fetchone()["n"]
        assert n == 0  # у легаси-группы действительно нет ни одной записи

        roots = {g.id for g in groups_v2.list_children(None)}
        assert legacy.id in roots
        assert v2_root.id in roots
        print("[OK] test_list_children_includes_legacy_groups_without_binding_row")
    finally:
        db.close(); os.unlink(path)


def test_add_member_and_list_members():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        points = MeteringPointRepo(db)
        g = groups.add("Группа 1")
        p1 = points.add("p1", "Точка 1")
        p2 = points.add("p2", "Точка 2")

        groups.add_member(g.id, p1.id)
        groups.add_member(g.id, p2.id)

        members = groups.list_members(g.id)
        assert {m.point_id for m in members} == {p1.id, p2.id}
        assert all(m.valid_to is None for m in members)
        print("[OK] test_add_member_and_list_members")
    finally:
        db.close(); os.unlink(path)


def test_add_member_duplicate_open_raises():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        points = MeteringPointRepo(db)
        g = groups.add("Группа 1")
        p1 = points.add("p1", "Точка 1")
        groups.add_member(g.id, p1.id)
        try:
            groups.add_member(g.id, p1.id)
            assert False, "ожидали ValueError (дублирующее открытое членство)"
        except ValueError as e:
            assert "уже состоит" in str(e)
        print("[OK] test_add_member_duplicate_open_raises")
    finally:
        db.close(); os.unlink(path)


def test_add_member_same_point_multiple_groups_allowed():
    """ТЗ §4.4: "разрешить одну точку в нескольких группах" — в отличие от
    point_bindings/primary здесь нет эксклюзивности по точке."""
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        points = MeteringPointRepo(db)
        g1 = groups.add("Группа 1")
        g2 = groups.add("Группа 2")
        p1 = points.add("p1", "Точка 1")

        groups.add_member(g1.id, p1.id)
        groups.add_member(g2.id, p1.id)  # не должно бросить исключение

        group_ids = {m.group_id for m in groups.list_groups_for_point(p1.id)}
        assert group_ids == {g1.id, g2.id}
        print("[OK] test_add_member_same_point_multiple_groups_allowed")
    finally:
        db.close(); os.unlink(path)


def test_remove_member():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        points = MeteringPointRepo(db)
        g = groups.add("Группа 1")
        p1 = points.add("p1", "Точка 1")
        groups.add_member(g.id, p1.id, valid_from=1000)

        groups.remove_member(g.id, p1.id, at=2000)

        open_members = groups.list_members(g.id, include_closed=False)
        assert open_members == []
        all_members = groups.list_members(g.id, include_closed=True)
        assert len(all_members) == 1 and all_members[0].valid_to == 2000

        # можно снова добавить точку после закрытия
        groups.add_member(g.id, p1.id, valid_from=2000)
        assert len(groups.list_members(g.id)) == 1
        print("[OK] test_remove_member")
    finally:
        db.close(); os.unlink(path)


def test_remove_member_without_open_membership_raises():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        points = MeteringPointRepo(db)
        g = groups.add("Группа 1")
        p1 = points.add("p1", "Точка 1")
        try:
            groups.remove_member(g.id, p1.id)
            assert False, "ожидали ValueError"
        except ValueError as e:
            assert "не состоит" in str(e)
        print("[OK] test_remove_member_without_open_membership_raises")
    finally:
        db.close(); os.unlink(path)


def test_resolve_effective_members_dedup_and_provenance():
    """Самый ответственный тест: точка p1 прямо состоит и в родительской
    группе, и в дочерней -- при разрешении состава родителя должна
    попасть в результат РОВНО ОДИН раз, но via должен показать оба пути.
    Точка p2 состоит только в дочерней группе (косвенно наследуется).
    Точка p3 состоит в независимой группе вне поддерева -- не должна
    попасть в результат вовсе."""
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        points = MeteringPointRepo(db)

        root = groups.add("Объект")
        child = groups.add("Цех", parent_id=root.id)
        other = groups.add("Другая ветка")

        p1 = points.add("p1", "Точка 1")  # в root И в child
        p2 = points.add("p2", "Точка 2")  # только в child
        p3 = points.add("p3", "Точка 3")  # только в other -- не должна попасть

        groups.add_member(root.id, p1.id)
        groups.add_member(child.id, p1.id)
        groups.add_member(child.id, p2.id)
        groups.add_member(other.id, p3.id)

        result = groups.resolve_effective_members(root.id)
        by_point = {r["point_id"]: r["via"] for r in result}

        assert set(by_point.keys()) == {p1.id, p2.id}  # p1 один раз, p3 отсутствует
        assert sorted(by_point[p1.id]) == sorted([root.id, child.id])
        assert by_point[p2.id] == [child.id]
        print("[OK] test_resolve_effective_members_dedup_and_provenance")
    finally:
        db.close(); os.unlink(path)


def test_resolve_effective_members_respects_at_time():
    """Членство, закрытое ДО момента at, не должно учитываться в составе
    на этот момент; членство, открытое ПОСЛЕ at, тоже не должно."""
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        points = MeteringPointRepo(db)
        g = groups.add("Группа")
        p1 = points.add("p1", "Точка 1")
        p2 = points.add("p2", "Точка 2")

        groups.add_member(g.id, p1.id, valid_from=1000)
        groups.remove_member(g.id, p1.id, at=2000)
        groups.add_member(g.id, p2.id, valid_from=3000)

        at_1500 = groups.resolve_effective_members(g.id, at=1500)
        assert {r["point_id"] for r in at_1500} == {p1.id}

        at_2500 = groups.resolve_effective_members(g.id, at=2500)
        assert {r["point_id"] for r in at_2500} == set()

        at_3500 = groups.resolve_effective_members(g.id, at=3500)
        assert {r["point_id"] for r in at_3500} == {p2.id}
        print("[OK] test_resolve_effective_members_respects_at_time")
    finally:
        db.close(); os.unlink(path)


def test_resolve_effective_members_missing_group_raises():
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)
        try:
            groups.resolve_effective_members(99999)
            assert False, "ожидали ValueError"
        except ValueError as e:
            assert "не найдена" in str(e)
        print("[OK] test_resolve_effective_members_missing_group_raises")
    finally:
        db.close(); os.unlink(path)


def test_add_marks_v2_domain_generation():
    db, path = make_db()
    try:
        kv = KvRepo(db)
        assert domain_generation.get_domain_generation(kv) == domain_generation.LEGACY_GENERATION

        groups = GroupRepoV2(db)
        groups.add("Группа 1")

        assert domain_generation.get_domain_generation(kv) == domain_generation.CURRENT_GENERATION
        assert domain_generation.get_v2_first_write_marker(kv) is not None
        print("[OK] test_add_marks_v2_domain_generation")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_add_root_group_no_parent()
    test_add_child_group_sets_parent_and_binding()
    test_add_duplicate_name_raises()
    test_add_missing_parent_raises()
    test_category_scope_blocks_cross_category_parent()
    test_category_scope_allows_when_either_category_none()
    test_set_parent_updates_cache_and_history()
    test_set_parent_self_parent_cycle()
    test_set_parent_deep_cycle_detection()
    test_path_to_root()
    test_list_children_includes_legacy_groups_without_binding_row()
    test_add_member_and_list_members()
    test_add_member_duplicate_open_raises()
    test_add_member_same_point_multiple_groups_allowed()
    test_remove_member()
    test_remove_member_without_open_membership_raises()
    test_resolve_effective_members_dedup_and_provenance()
    test_resolve_effective_members_respects_at_time()
    test_resolve_effective_members_missing_group_raises()
    test_add_marks_v2_domain_generation()
    print("[ALL OK] test_step24_group_repo_v2")

#!/usr/bin/env python3
"""Шаг 62 (партия находок 0.22.3, П9): миграция 006 `phase_aggregates`,
откат на 0.22.2 и страж поколений для пофазных привязок
(docs/design-phase-aggregates.md §3–§4).

Что проверяется (числа и состояния заданы заранее):

1. Миграция 006 применяется на БД схемы 5 с данными: версия 6, таблица
   `phase_aggregates` и её индекс на месте, ни одна строка существующих таблиц
   не потеряна и не изменена, `period_aggregates` не тронута.
2. **Откат на 0.22.2 (обновление до 0.22.3 → откат).** БД, мигрированная
   новым кодом и наполненная (итоги прибора + часы фаз), открывается КОДОМ
   0.22.2 — деревом пакета без файла `006…` (то, что 0.22.2 и видит в своей
   папке миграций; настоящее дерево 0.22.2 можно подставить через
   `WBEM_OLD_CODE_DIR`): сервис поднимается (Database.open), версия схемы
   остаётся 6, миграции не переприменяются, данные целы; запись часа
   ПО-СТАРОМУ (`ON CONFLICT(meter_id, period_type, period_start)`) проходит
   и перезаписывает ровно свою строку; удаление счётчика старым кодом
   уносит и его пофазные строки (каскад) без ошибки.
3. **Поколения.** До первой пофазной привязки БД требует читателя поколения 2:
   страж `check-generation.sh` пускает код поколения 2 (откат разрешён).
   Первая привязка `total_3p` ничего не меняет. Первая пофазная привязка
   (`open_binding` или `replace_meter` на фазу) в той же транзакции
   поднимает `domain_model_generation` и `minimum_reader_generation` до 3:
   страж пускает код поколения 3 и БЛОКИРУЕТ код поколения 2 внятным текстом.
   Отказанная привязка (конфликт) поколение не поднимает. Повторная пофазная
   привязка идемпотентна. Поколение, объявленное в `__init__.py`, равно
   `domain_generation.CODE_GENERATION`.

Самостоятельный скрипт (не pytest):
    python tests/test_step62_phase_migration_rollback.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import db as db_module  # noqa: E402
from wb_energy_meter import domain_generation  # noqa: E402
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate  # noqa: E402
from wb_energy_meter.binding_service import BindingConflict, PointBindingRepo  # noqa: E402
from wb_energy_meter.db import Database  # noqa: E402
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo  # noqa: E402
from wb_energy_meter.repo import GroupRepo, KvRepo, MeterRepo  # noqa: E402

PKG = os.path.join(REPO_ROOT, "wb_energy_meter")
MIGRATIONS = os.path.join(PKG, "migrations")
GUARD = os.path.join(REPO_ROOT, "scripts", "check-generation.sh")
HOUR = 3600
T0 = 1_700_002_800  # кратно 3600


def hour_row(meter_id, start, kwh, flag="ok"):
    return HourlyAggregate(
        meter_id=meter_id, period_start=start, period_end=start + HOUR,
        ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
        p_avg=None, p_max=None, samples_count=3, quality_flag=flag,
        computed_at=start)


def new_path(tmp, name="state.db"):
    return os.path.join(tmp, name)


# ---------------------------------------------------------------------
# 1. миграция 006 на БД схемы 5 с данными
# ---------------------------------------------------------------------

def test_migration_006_applies_on_populated_v5_db():
    with tempfile.TemporaryDirectory() as tmp:
        old_dir = os.path.join(tmp, "migrations_v5")
        os.makedirs(old_dir)
        for name in sorted(os.listdir(MIGRATIONS)):
            if name.endswith(".sql") and int(name.split("_", 1)[0]) <= 5:
                shutil.copy(os.path.join(MIGRATIONS, name), os.path.join(old_dir, name))

        path = new_path(tmp)
        saved = db_module._MIGRATIONS_DIR
        db_module._MIGRATIONS_DIR = type(saved)(old_dir)
        try:
            db = Database(path=path)
            db.open()
            assert db.current_schema_version() == 5
            meters = MeterRepo(db, GroupRepo(db))
            m = meters.add("wb-map3e_16", "Прибор 16")
            repo = AggregateRepo(db)
            repo.upsert_many([hour_row(m.id, T0 + i * HOUR, 1.5 + i) for i in range(6)])
            before = _snapshot(db)
            db.close()
        finally:
            db_module._MIGRATIONS_DIR = saved

        db = Database(path=path)
        db.open()
        try:
            assert db.current_schema_version() == 6, db.current_schema_version()
            tables = {r["name"] for r in db.conn().execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert "phase_aggregates" in tables, tables
            idx = {r["name"] for r in db.conn().execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
            assert "idx_phase_aggr_period_start" in idx, idx
            row = db.conn().execute(
                "SELECT name FROM schema_migrations WHERE version = 6").fetchone()
            assert row is not None and row["name"] == "phase_aggregates"
            assert _snapshot(db) == before, "миграция 006 изменила существующие данные"
            n = db.conn().execute("SELECT COUNT(*) AS n FROM phase_aggregates").fetchone()["n"]
            assert n == 0
            # фаза вне L1..L3 запрещена схемой
            try:
                db.conn().execute(
                    "INSERT INTO phase_aggregates (meter_id, phase, period_start, "
                    "period_end, quality_flag, computed_at) VALUES (?, 'L4', 0, 3600, 'ok', 0)",
                    (m.id,))
                raise AssertionError("фаза L4 должна быть отвергнута CHECK")
            except sqlite3.IntegrityError:
                pass
        finally:
            db.close()
    print("[OK] миграция 006: версия 6, таблица и индекс, данные 001–005 не тронуты, "
          "CHECK по фазам")


def _snapshot(db):
    out = {}
    for t in ("meters", "period_aggregates", "kv", "meter_groups"):
        rows = db.conn().execute("SELECT * FROM %s ORDER BY 1, 2, 3" % t).fetchall()
        out[t] = [tuple(r) for r in rows]
    return out


# ---------------------------------------------------------------------
# 2. откат на 0.22.2
# ---------------------------------------------------------------------

OLD_CODE_PROBE = r'''
import json, sqlite3, sys
from wb_energy_meter.db import Database
import wb_energy_meter
db = Database(path=sys.argv[1])
db.open()                                   # «сервис поднимается»
out = {"version": wb_energy_meter.__version__,
       "schema": db.current_schema_version()}
c = db.conn()
out["period_rows"] = c.execute("SELECT COUNT(*) FROM period_aggregates").fetchone()[0]
out["phase_rows"] = c.execute("SELECT COUNT(*) FROM phase_aggregates").fetchone()[0]
out["migrations"] = [r[0] for r in c.execute(
    "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
# запись часа ПО-СТАРОМУ: ключ конфликта — (meter_id, period_type, period_start)
mid = c.execute("SELECT id FROM meters WHERE device_id='wb-map3e_16'").fetchone()[0]
c.execute(
    "INSERT INTO period_aggregates (meter_id, period_type, period_start, period_end, "
    "ap_energy_start, ap_energy_end, ap_energy_delta, p_avg, p_max, samples_count, "
    "quality_flag, computed_at) VALUES (?, 'hour', ?, ?, 0, 99, 99, NULL, NULL, 1, 'ok', 1) "
    "ON CONFLICT(meter_id, period_type, period_start) DO UPDATE SET "
    "ap_energy_delta = excluded.ap_energy_delta, ap_energy_end = excluded.ap_energy_end",
    (mid, int(sys.argv[2]), int(sys.argv[2]) + 3600))
out["overwritten"] = c.execute(
    "SELECT ap_energy_delta FROM period_aggregates WHERE meter_id=? AND period_start=?",
    (mid, int(sys.argv[2]))).fetchone()[0]
out["period_rows_after"] = c.execute("SELECT COUNT(*) FROM period_aggregates").fetchone()[0]
# удаление счётчика старым кодом: каскад уносит и пофазные строки
c.execute("PRAGMA foreign_keys = ON")
c.execute("DELETE FROM period_aggregates WHERE meter_id=?", (mid,))
c.execute("DELETE FROM meters WHERE id=?", (mid,))
out["phase_rows_after_delete"] = c.execute("SELECT COUNT(*) FROM phase_aggregates").fetchone()[0]
db.close()
print(json.dumps(out))
'''


def _old_code_tree(tmp):
    """Дерево пакета «0.22.2»: настоящее (WBEM_OLD_CODE_DIR) или копия
    текущего без миграции 006 — ровно то, что 0.22.2 видит в своей папке."""
    real = os.environ.get("WBEM_OLD_CODE_DIR")
    if real:
        assert os.path.isdir(os.path.join(real, "wb_energy_meter")), real
        return real, "настоящее дерево 0.22.2"
    root = os.path.join(tmp, "old_tree")
    shutil.copytree(
        PKG, os.path.join(root, "wb_energy_meter"),
        ignore=shutil.ignore_patterns("__pycache__", "static", "*.pyc"))
    os.unlink(os.path.join(root, "wb_energy_meter", "migrations", "006_phase_aggregates.sql"))
    return root, "копия пакета без миграции 006"


def test_rollback_to_previous_code_keeps_service_up_and_data_intact():
    with tempfile.TemporaryDirectory() as tmp:
        path = new_path(tmp)
        db = Database(path=path)
        db.open()
        meters = MeterRepo(db, GroupRepo(db))
        m = meters.add("wb-map3e_16", "Прибор 16")
        total = AggregateRepo(db)
        total.upsert_many([hour_row(m.id, T0 + i * HOUR, 3.5) for i in range(4)])
        for ph, kwh in (("L1", 1.0), ("L2", 2.0), ("L3", 0.5)):
            total.for_profile("phase_" + ph.lower()).upsert_many(
                [hour_row(m.id, T0 + i * HOUR, kwh) for i in range(4)])
        assert db.current_schema_version() == 6
        db.close()

        tree, kind = _old_code_tree(tmp)
        env = dict(os.environ)
        env["PYTHONPATH"] = tree
        # cwd=tree: `python -c` кладёт cwd первым в sys.path — импортируется
        # пакет «старого кода», а не текущий.
        r = subprocess.run(
            [sys.executable, "-c", OLD_CODE_PROBE, path, str(T0 + HOUR)],
            capture_output=True, text=True, env=env, timeout=60, cwd=tree)
        assert r.returncode == 0, "старый код не поднялся: %s %s" % (r.stdout, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert out["schema"] == 6, out
        assert out["migrations"] == [1, 2, 3, 4, 5, 6], ("миграции переприменялись?", out)
        assert out["period_rows"] == 4 and out["phase_rows"] == 12, out
        assert out["overwritten"] == 99.0, out
        assert out["period_rows_after"] == 4, "старая запись должна перезаписать ровно свою строку"
        assert out["phase_rows_after_delete"] == 0, "каскад должен унести пофазные строки счётчика"
    print("[OK] откат на 0.22.2 (%s): сервис поднимается, схема 6, миграции не "
          "переприменяются, данные целы, старая запись часа и удаление счётчика "
          "работают" % kind)


# ---------------------------------------------------------------------
# 3. поколения
# ---------------------------------------------------------------------

def _write_init(path, code_generation):
    with open(path, "w", encoding="utf-8") as f:
        f.write('__version__ = "x"\n__code_generation__ = %d\n' % code_generation)


def _guard(db_path, tmp, code_generation):
    init_py = os.path.join(tmp, "init_gen%d.py" % code_generation)
    _write_init(init_py, code_generation)
    return subprocess.run(["sh", GUARD, db_path, init_py], capture_output=True,
                          text=True, timeout=30)


class World:
    def __init__(self, tmp):
        self.path = new_path(tmp)
        self.db = Database(path=self.path)
        self.db.open()
        self.kv = KvRepo(self.db)
        self.meters = MeterRepo(self.db, GroupRepo(self.db))
        self.sources = MeterSourceRepo(self.db)
        self.points = MeteringPointRepo(self.db)
        self.bindings = PointBindingRepo(self.db)

    def device(self, code):
        m = self.meters.add(code, code)
        return self.sources.open_source(m.id, "wb8-main", code)

    def gens(self):
        return (domain_generation.get_domain_generation(self.kv),
                domain_generation.get_min_reader_generation(self.kv))


def test_declared_code_generation_is_in_sync():
    text = open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    declared = int(re.search(r"__code_generation__\s*=\s*(\d+)", text).group(1))
    assert declared == domain_generation.CODE_GENERATION == domain_generation.PHASE_GENERATION == 3
    assert domain_generation.CURRENT_GENERATION == 2, (
        "первая v2-запись по-прежнему поколения 2 — откат до пофазных привязок разрешён")
    print("[OK] __code_generation__ в __init__.py = CODE_GENERATION = 3, CURRENT_GENERATION = 2")


def test_generation_rises_only_with_first_phase_binding():
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        try:
            src = w.device("wb-map3e_16")
            p_total = w.points.add("T", "Общая")
            p_l1 = w.points.add("P1", "Фаза 1")
            p_l2 = w.points.add("P2", "Фаза 2")

            # создание точек — первая v2-запись: поколение 2 (как в 0.22.2)
            assert w.gens() == (2, 2), w.gens()
            assert _guard(w.path, tmp, 2).returncode == 0

            # привязка total_3p поколение НЕ меняет
            w.bindings.open_binding(p_total.id, src.id, "total_3p", valid_from=0)
            assert w.gens() == (2, 2), w.gens()
            src2 = w.device("wb-map3e_17")
            assert _guard(w.path, tmp, 2).returncode == 0, "откат разрешён до пофазных привязок"

            # отказанная пофазная привязка (прибор занят total_3p) — поколение не поднимает
            try:
                w.bindings.open_binding(p_l1.id, src.id, "phase_l1", valid_from=0)
                raise AssertionError("ожидался BindingConflict: total_3p занимает все фазы")
            except BindingConflict:
                pass
            assert w.gens() == (2, 2), "отказ не должен поднимать поколение"

            # первая пофазная привязка — в той же транзакции поднимает до 3
            w.bindings.open_binding(p_l1.id, src2.id, "phase_l1", valid_from=0)
            assert w.gens() == (3, 3), w.gens()
            assert domain_generation.get_v2_first_write_marker(w.kv) is not None, (
                "маркер первой v2-записи согласован с поколением")

            blocked = _guard(w.path, tmp, 2)
            assert blocked.returncode == 1, (blocked.returncode, blocked.stdout, blocked.stderr)
            text = blocked.stdout + blocked.stderr
            assert "3" in text and "2" in text, text
            assert _guard(w.path, tmp, 3).returncode == 0

            # идемпотентно: вторая фаза не меняет ключи (в т.ч. метку времени)
            before = w.db.conn().execute(
                "SELECT key, value, updated_at FROM kv ORDER BY key").fetchall()
            w.bindings.open_binding(p_l2.id, src2.id, "phase_l2", valid_from=0)
            after = w.db.conn().execute(
                "SELECT key, value, updated_at FROM kv ORDER BY key").fetchall()
            assert [tuple(r) for r in before] == [tuple(r) for r in after]
        finally:
            w.db.close()
    print("[OK] поколение: total_3p не трогает, отказ не трогает, первая пофазная привязка "
          "→ 3, страж блокирует код поколения 2 и пускает 3, повтор идемпотентен")


def test_replace_meter_to_phase_raises_generation():
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        try:
            a, b = w.device("wb-map3e_20"), w.device("wb-map3e_21")
            p = w.points.add("P", "Точка")
            w.bindings.open_binding(p.id, a.id, "total_3p", valid_from=0)
            assert w.gens() == (2, 2)
            w.bindings.replace_meter(p.id, b.id, at=1000, channel_profile="phase_l3")
            assert w.gens() == (3, 3), w.gens()
            assert _guard(w.path, tmp, 2).returncode == 1
        finally:
            w.db.close()
    print("[OK] замена прибора на пофазный профиль поднимает поколение до 3")


def test_mark_phase_binding_never_lowers_generation():
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        try:
            with w.db.transaction() as c:
                c.execute("INSERT INTO kv (key, value, updated_at) VALUES "
                          "('minimum_reader_generation', '5', 1)")
            with w.db.transaction() as c:
                assert domain_generation.mark_phase_binding(c) is False
            assert domain_generation.get_min_reader_generation(w.kv) == 5
        finally:
            w.db.close()
    print("[OK] mark_phase_binding не понижает более высокое поколение")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print("\nВСЁ ЗЕЛЁНОЕ: %d проверок" % len(tests))

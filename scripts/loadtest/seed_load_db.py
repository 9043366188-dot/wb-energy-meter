#!/usr/bin/env python3
"""Партия 10, этап E (docs/TZ-batch10-reliability-and-load.md §5):
генератор нагрузочной БД для scripts/loadtest/measure.py.

Создаёт **ОТДЕЛЬНУЮ**, новую с нуля БД (путь передаётся аргументом,
по умолчанию — временный файл рядом со скриптом; НИКОГДА не боевой
`state.db`) со следующими данными:

- 100 точек учёта (`metering_points`) с приборами (`meters`),
  источниками (`meter_sources`) и открытыми привязками (`point_bindings`,
  role='primary', channel_profile='total_3p') — история привязки
  открыта на год с запасом до начала генерируемых агрегатов;
- радиальная электрическая сеть: 1 ввод (source) -> 5 щитов (panel) ->
  94 потребителя (load), итого 100 узлов и 99 связей; ВСЕ 99 связей
  измеряемые (primary_point_id проставлен). Точек — 100, связей — 99:
  одна точка (последняя) сознательно остаётся без электрической связи —
  это "лишняя" точка учёта, которая есть в базе, но ещё не заведена в
  схему сети (нормальная ситуация на реальном объекте на этапе
  настройки), а не ошибка генератора;
- 10 планов (`site_plans`, plan_kind='single_line', без файла
  изображения — картинка не нужна для нагрузочного теста) по 20
  элементов (`plan_items`, kind='node') и 15 линий (`plan_edge_views`)
  на каждом; элементы каждого плана — связный кусок сети (BFS от
  случайного узла), поэтому у большинства линий оба конца попадают в
  свой же план (from_item_id/to_item_id заполнены), недостающие до 15
  линии на плане берутся из остальной сети (тогда концы NULL — линия
  показана без точной привязки к отрисованным элементам, это
  допустимо по схеме: from_item_id/to_item_id ON DELETE SET NULL);
- год почасовых агрегатов (`period_aggregates`) на все 100 приборов —
  100 * 8760 = 876 000 строк. Модель нагрузки: у каждого потребителя
  свой базовый уровень мощности с суточным профилем (день/ночь) и
  шумом; агрегат щита = сумма агрегатов его потребителей (+ до ±1%
  независимой погрешности измерения), агрегат ввода = сумма щитов
  (+ до ±1%) — небаланс объекта получается небольшим и правдоподобным,
  а не тождественным нулём и не хаосом.

Только стандартная библиотека — без numpy/pandas и т.п. (см. AGENTS.md
и docs/TZ-batch10-reliability-and-load.md §6: "только stdlib плюс
flask, paho-mqtt, pyyaml").

Использование:
    python scripts/loadtest/seed_load_db.py --db-path /tmp/loadtest.db
    python scripts/loadtest/seed_load_db.py --db-path /tmp/loadtest.db --days 30   # быстрый прогон для отладки самого генератора
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.binding_service import PointBindingRepo
from wb_energy_meter.topology_service import ElectricalNodeRepo, ElectricalEdgeRepo
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate

N_POINTS = 100
N_PANELS = 5
N_CONSUMERS = 94  # + 1 source = 100 узлов, 100 точек; 99 связей (source->panel x5, panel->consumer x94)
N_PLANS = 10
ELEMENTS_PER_PLAN = 20
LINES_PER_PLAN = 15
SEED = 20260927  # детерминированный прогон — воспроизводимые числа в отчёте


def _align_hour(ts: int) -> int:
    return (ts // 3600) * 3600


def build_topology(node_repo: ElectricalNodeRepo, edge_repo: ElectricalEdgeRepo, points):
    """Строит 1 ввод -> 5 щитов -> 94 потребителя, все линии измеряемые.
    Возвращает (nodes_by_role, edges_meta) — nodes_by_role для раскладки
    планов, edges_meta = [(edge_id, from_node_id, to_node_id, point_id)]."""
    source = node_repo.add("IN-1", "Ввод", "source")

    panel_sizes = _split_evenly(N_CONSUMERS, N_PANELS)
    panels = []
    for i in range(N_PANELS):
        panels.append(node_repo.add(f"P-{i+1}", f"Щит {i+1}", "panel"))

    consumers = []
    for i in range(N_CONSUMERS):
        consumers.append(node_repo.add(f"C-{i+1}", f"Потребитель {i+1}", "load"))

    # points[0..98] — на 99 связей, points[99] — намеренно без связи (см. докстринг).
    assert len(points) == N_POINTS
    line_points = points[:N_POINTS - 1]
    spare_point = points[-1]

    edge_ids = []
    edges_meta = []  # (from_node_id, to_node_id, point_id, kind) kind: 'input'|'panel'|'consumer'
    pt_iter = iter(line_points)

    for i, panel in enumerate(panels):
        p = next(pt_iter)
        e = edge_repo.add_draft(
            source.id, panel.id, code=f"E-IN-{i+1}", name=f"Ввод -> Щит {i+1}",
            primary_point_id=p.id, phase_count=3, rated_current_a=100.0)
        edge_ids.append(e.id)
        edges_meta.append((e.id, source.id, panel.id, p.id, "input"))

    consumer_idx = 0
    panel_children = {panel.id: [] for panel in panels}
    for panel, size in zip(panels, panel_sizes):
        for _ in range(size):
            c = consumers[consumer_idx]
            consumer_idx += 1
            p = next(pt_iter)
            e = edge_repo.add_draft(
                panel.id, c.id, code=f"E-{c.code}", name=f"{panel.name} -> {c.name}",
                primary_point_id=p.id, phase_count=1, rated_current_a=25.0)
            edge_ids.append(e.id)
            edges_meta.append((e.id, panel.id, c.id, p.id, "consumer"))
            panel_children[panel.id].append(c.id)

    edge_repo.publish_edges(edge_ids)

    all_node_ids = [source.id] + [p.id for p in panels] + [c.id for c in consumers]
    return {
        "source": source, "panels": panels, "consumers": consumers,
        "all_node_ids": all_node_ids, "panel_children": panel_children,
        "spare_point": spare_point,
    }, edges_meta


def _split_evenly(total, buckets):
    base, rem = divmod(total, buckets)
    return [base + (1 if i < rem else 0) for i in range(buckets)]


def seed_points_and_bindings(db, meter_repo, source_repo, point_repo, binding_repo,
                              n, binding_start_ts):
    """Создаёт n точек с приборами/источниками/привязками. Возвращает
    список MeteringPoint (в порядке создания) и список meter_id (тот
    же порядок) для последующей генерации агрегатов."""
    points = []
    meter_ids = []
    for i in range(n):
        device_id = f"loadtest-meter-{i+1:03d}"
        meter = meter_repo.add(device_id, f"Нагрузочный счётчик {i+1}", role="consumer")
        source = source_repo.open_source(meter.id, "loadtest", device_id)
        point = point_repo.add(f"LT-{i+1:03d}", f"Точка нагрузочного теста {i+1}")
        binding_repo.open_binding(
            point.id, source.id, "total_3p", role="primary",
            valid_from=binding_start_ts)
        points.append(point)
        meter_ids.append(meter.id)
    return points, meter_ids


def seed_plans(db, node_repo, edge_repo, all_node_ids, edges_meta):
    """10 планов по 20 элементов (узлы, связный кусок сети через BFS) и
    15 линий каждый (см. докстринг модуля)."""
    adjacency = {nid: set() for nid in all_node_ids}
    for edge_id, frm, to, point_id, kind in edges_meta:
        adjacency[frm].add(to)
        adjacency[to].add(frm)
    all_edge_ids = [e[0] for e in edges_meta]

    now = int(time.time())
    rng = random.Random(SEED + 1)

    with db.transaction() as c:
        for plan_no in range(1, N_PLANS + 1):
            cur = c.execute(
                "INSERT INTO site_plans "
                "(name, plan_kind, image_file, image_width, image_height, "
                "canvas_width, canvas_height, canvas_revision, is_default, "
                "created_at, updated_at) "
                "VALUES (?, 'single_line', NULL, NULL, NULL, 2000, 2000, 1, 0, ?, ?)",
                (f"Нагрузочный план {plan_no}", now, now),
            )
            plan_id = cur.lastrowid

            # BFS от случайного узла — связный кусок сети из ELEMENTS_PER_PLAN узлов.
            start = rng.choice(all_node_ids)
            visited = [start]
            seen = {start}
            frontier = [start]
            while frontier and len(visited) < ELEMENTS_PER_PLAN:
                nxt = []
                for nid in frontier:
                    for nb in sorted(adjacency[nid]):
                        if nb not in seen:
                            seen.add(nb)
                            visited.append(nb)
                            nxt.append(nb)
                            if len(visited) >= ELEMENTS_PER_PLAN:
                                break
                    if len(visited) >= ELEMENTS_PER_PLAN:
                        break
                frontier = nxt
            node_ids_on_plan = visited[:ELEMENTS_PER_PLAN]

            item_id_by_node = {}
            for i, nid in enumerate(node_ids_on_plan):
                geometry = json.dumps({"x": 80 + (i % 5) * 150, "y": 80 + (i // 5) * 150})
                cur = c.execute(
                    "INSERT INTO plan_items "
                    "(plan_id, kind, point_id, location_id, group_id, node_id, "
                    "target_plan_id, geometry, coord_space, image_version_id, "
                    "label, sort_order, revision_id, archived_at, created_at, updated_at) "
                    "VALUES (?, 'node', NULL, NULL, NULL, ?, NULL, ?, 'canvas_xy_v2', "
                    "NULL, NULL, ?, NULL, NULL, ?, ?)",
                    (plan_id, nid, geometry, i, now, now),
                )
                item_id_by_node[nid] = cur.lastrowid

            node_set = set(node_ids_on_plan)
            internal_edges = [
                eid for (eid, frm, to, pid, kind) in edges_meta
                if frm in node_set and to in node_set
            ]
            rng.shuffle(internal_edges)
            chosen = internal_edges[:LINES_PER_PLAN]
            if len(chosen) < LINES_PER_PLAN:
                remaining = [eid for eid in all_edge_ids if eid not in chosen]
                rng.shuffle(remaining)
                chosen += remaining[:LINES_PER_PLAN - len(chosen)]

            edge_endpoints = {eid: (frm, to) for (eid, frm, to, pid, kind) in edges_meta}
            for eid in chosen:
                frm, to = edge_endpoints[eid]
                from_item = item_id_by_node.get(frm)
                to_item = item_id_by_node.get(to)
                c.execute(
                    "INSERT INTO plan_edge_views "
                    "(plan_id, edge_id, from_item_id, to_item_id, waypoints, "
                    "view_kind, confirmed_at, revision_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, 'structural', NULL, NULL, ?, ?)",
                    (plan_id, eid, from_item, to_item, now, now),
                )


def generate_aggregates(db, meter_ids, source_meter_id, panel_meter_ids, consumer_meter_ids,
                         panel_children_meters, hours, period_start0):
    """Год почасовых агрегатов, снизу вверх: потребитель -> щит -> ввод
    (см. докстринг модуля про модель нагрузки)."""
    rng = random.Random(SEED)
    agg_repo = AggregateRepo(db)

    base_kw = {mid: rng.uniform(0.4, 4.5) for mid in consumer_meter_ids}
    cumulative = {mid: rng.uniform(1000.0, 50000.0) for mid in meter_ids}

    t0 = time.time()
    total_rows = 0
    for panel_idx, panel_meter_id in enumerate(panel_meter_ids):
        members = panel_children_meters[panel_meter_id]
        panel_rows = []
        consumer_rows_by_meter = {mid: [] for mid in members}
        for h in range(hours):
            period_start = period_start0 + h * 3600
            period_end = period_start + 3600
            hour_of_day = time.localtime(period_start).tm_hour
            day_factor = 1.6 if 7 <= hour_of_day < 23 else 0.45

            panel_sum = 0.0
            for mid in members:
                delta = base_kw[mid] * day_factor * rng.uniform(0.85, 1.15)
                delta = max(0.0, round(delta, 4))
                start_v = cumulative[mid]
                cumulative[mid] += delta
                consumer_rows_by_meter[mid].append(HourlyAggregate(
                    meter_id=mid, period_start=period_start, period_end=period_end,
                    ap_energy_start=round(start_v, 3), ap_energy_end=round(cumulative[mid], 3),
                    ap_energy_delta=delta, p_avg=delta, p_max=round(delta * rng.uniform(1.05, 1.4), 4),
                    samples_count=12, quality_flag="ok", computed_at=period_end,
                ))
                panel_sum += delta

            panel_delta = round(panel_sum * rng.uniform(0.99, 1.01), 4)
            start_v = cumulative[panel_meter_id]
            cumulative[panel_meter_id] += panel_delta
            panel_rows.append(HourlyAggregate(
                meter_id=panel_meter_id, period_start=period_start, period_end=period_end,
                ap_energy_start=round(start_v, 3), ap_energy_end=round(cumulative[panel_meter_id], 3),
                ap_energy_delta=panel_delta, p_avg=panel_delta,
                p_max=round(panel_delta * rng.uniform(1.05, 1.3), 4),
                samples_count=12, quality_flag="ok", computed_at=period_end,
            ))

        for mid, rows in consumer_rows_by_meter.items():
            agg_repo.upsert_many(rows)
            total_rows += len(rows)
        agg_repo.upsert_many(panel_rows)
        total_rows += len(panel_rows)
        print(f"  щит {panel_idx+1}/{len(panel_meter_ids)}: "
              f"{len(members)} потребителей x {hours} ч + сам щит -- готово")

    # Ввод — сумма щитов по каждому часу (нужно перечитать panel_rows всех
    # щитов почасово; проще всего пересчитать напрямую из cumulative-подхода
    # тем же проходом по часам, храня промежуточные суммы).
    source_rows = []
    # Пересчитываем помесячно с нуля по тем же формулам детерминированного
    # RNG невозможно (состояние rng уже ушло вперёд) -- вместо этого читаем
    # только что записанные агрегаты щитов из БД посуточно.
    for h in range(hours):
        period_start = period_start0 + h * 3600
        period_end = period_start + 3600
        with db.read() as c:
            row = c.execute(
                "SELECT SUM(ap_energy_delta) AS s FROM period_aggregates "
                "WHERE period_type='hour' AND period_start=? AND meter_id IN ({})".format(
                    ",".join("?" * len(panel_meter_ids))),
                [period_start] + panel_meter_ids,
            ).fetchone()
        panels_sum = row["s"] or 0.0
        delta = round(panels_sum * rng.uniform(0.99, 1.01), 4)
        start_v = cumulative[source_meter_id]
        cumulative[source_meter_id] += delta
        source_rows.append(HourlyAggregate(
            meter_id=source_meter_id, period_start=period_start, period_end=period_end,
            ap_energy_start=round(start_v, 3), ap_energy_end=round(cumulative[source_meter_id], 3),
            ap_energy_delta=delta, p_avg=delta, p_max=round(delta * rng.uniform(1.05, 1.25), 4),
            samples_count=12, quality_flag="ok", computed_at=period_end,
        ))
    agg_repo.upsert_many(source_rows)
    total_rows += len(source_rows)

    elapsed = time.time() - t0
    print(f"  агрегаты: {total_rows} строк за {elapsed:.1f} с")
    return total_rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db-path", default=None,
                     help="путь к НОВОЙ отдельной БД (по умолчанию -- временный файл; "
                          "никогда не указывайте сюда боевой state.db)")
    ap.add_argument("--days", type=int, default=365,
                     help="сколько дней почасовых агрегатов генерировать (по умолчанию 365 "
                          "= ~876 тыс. строк на 100 приборов; меньше -- для отладки генератора)")
    args = ap.parse_args(argv)

    if args.db_path is None:
        import tempfile
        fd, path = tempfile.mkstemp(prefix="wbem_loadtest_", suffix=".db")
        os.close(fd)
        os.unlink(path)
        args.db_path = path

    if os.path.exists(args.db_path):
        print(f"ОШИБКА: {args.db_path} уже существует -- генератор создаёт НОВУЮ "
              f"базу с нуля, удалите файл или укажите другой путь.", file=sys.stderr)
        return 2

    print(f"Создаю нагрузочную БД: {args.db_path}")
    t_start = time.time()

    db = Database(path=args.db_path)
    t_open0 = time.time()
    db.open()
    t_open = time.time() - t_open0
    print(f"  db.open() (применение миграций 001-005 на пустой базе): {t_open:.3f} с")

    groups_repo = GroupRepo(db)
    meter_repo = MeterRepo(db, groups_repo)
    source_repo = MeterSourceRepo(db)
    point_repo = MeteringPointRepo(db)
    binding_repo = PointBindingRepo(db)
    node_repo = ElectricalNodeRepo(db)
    edge_repo = ElectricalEdgeRepo(db)

    hours = args.days * 24
    now_aligned = _align_hour(int(time.time()))
    period_start0 = now_aligned - hours * 3600
    binding_start_ts = period_start0 - 3600  # привязка открыта чуть раньше первого агрегата

    print(f"Создаю {N_POINTS} точек учёта с приборами/источниками/привязками ...")
    points, meter_ids = seed_points_and_bindings(
        db, meter_repo, source_repo, point_repo, binding_repo, N_POINTS, binding_start_ts)

    print(f"Строю топологию: 1 ввод -> {N_PANELS} щитов -> {N_CONSUMERS} потребителей ...")
    topo, edges_meta = build_topology(node_repo, edge_repo, points)

    point_id_to_meter_id = {p.id: mid for p, mid in zip(points, meter_ids)}

    # "Ввод" (source-узел) не измеряется своим отдельным прибором в этой
    # схеме (в реальности источник обычно тот же прибор, что и на первой
    # питающей линии, но здесь нужна отдельная запись для суммы всего
    # объекта) -- под неё используется именно ЗАПАСНАЯ 100-я точка,
    # намеренно не привязанная ни к одной электрической связи (см.
    # докстринг модуля): для неё это единственное назначение.
    source_meter_id = point_id_to_meter_id[topo["spare_point"].id]

    # panel_node_id -> meter_id прибора НА ЛИНИИ "ввод -> этот щит"
    # (kind='input'), и panel_meter_id -> [consumer_meter_id, ...] по
    # линиям "щит -> потребитель" (kind='consumer') -- один проход по
    # edges_meta вместо O(n^2) поиска.
    panel_node_to_meter = {}
    panel_meter_ids = []
    for eid, frm, to, point_id, kind in edges_meta:
        if kind == "input":
            mid = point_id_to_meter_id[point_id]
            panel_node_to_meter[to] = mid
            panel_meter_ids.append(mid)

    panel_children_meters = {mid: [] for mid in panel_meter_ids}
    consumer_meter_ids = []
    for eid, frm, to, point_id, kind in edges_meta:
        if kind != "consumer":
            continue
        mid = point_id_to_meter_id[point_id]
        consumer_meter_ids.append(mid)
        panel_mid = panel_node_to_meter[frm]
        panel_children_meters[panel_mid].append(mid)

    print(f"Планы: {N_PLANS} x ({ELEMENTS_PER_PLAN} элементов, {LINES_PER_PLAN} линий) ...")
    seed_plans(db, node_repo, edge_repo, topo["all_node_ids"], edges_meta)

    print(f"Агрегаты: {hours} часов x {N_POINTS} приборов ...")
    total_rows = generate_aggregates(
        db, meter_ids, source_meter_id, panel_meter_ids, consumer_meter_ids,
        panel_children_meters, hours, period_start0)

    db_size = os.path.getsize(args.db_path)
    elapsed_total = time.time() - t_start
    stats = {
        "db_path": args.db_path,
        "points": N_POINTS,
        "nodes": 1 + N_PANELS + N_CONSUMERS,
        "edges": N_PANELS + N_CONSUMERS,
        "plans": N_PLANS,
        "aggregate_rows": total_rows,
        "hours_per_meter": hours,
        "db_size_bytes": db_size,
        "migration_time_s_on_empty_db": t_open,
        "seed_elapsed_s": elapsed_total,
        "period_start0": period_start0,
        "period_end0": now_aligned,
    }
    db.close()

    print("\nГотово:")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

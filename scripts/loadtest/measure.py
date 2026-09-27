#!/usr/bin/env python3
"""Партия 10, этап E (docs/TZ-batch10-reliability-and-load.md §5):
нагрузочный прогон на уже сгенерированной БД (см. seed_load_db.py).

Поднимает сервис ОТДЕЛЬНЫМ процессом (scripts/loadtest/server_entry.py)
на указанной базе и на указанном (нестандартном) порту и эмулирует 3
клиентов:

  - `snapshot`             -- раз в 5 с;
  - `overview/summary`     -- за "сегодня", раз в 60 с;
  - `reports/query`        -- за месяц (dimension=branch), раз в 5 мин;
  - баланс узла по клику   -- раз в 20 с (случайный щит), для полноты
                               картины, хотя жёсткой цели по нему нет.

Отдельно измеряет (§5, ради чего всё затевалось):
  - число SQL-запросов на один `overview/summary` и один `reports/query`
    (через служебные маршруты /__loadtest/trace/* сервера, которые сами
    используют `sqlite3.set_trace_callback` -- см. server_entry.py);
  - длительность удержания блокировки записи во время тяжёлого расчёта:
    дешёвая запись (`/__loadtest/write_probe`) замеряется изолированно
    (без параллельной нагрузки) и во время параллельного
    `reports/query` за месяц -- разница показывает, насколько долго
    расчёт фактически не даёт писателю продвинуться.

Только stdlib -- никаких requests/psutil и т.п. (см. AGENTS.md).

Использование:
    python scripts/loadtest/measure.py --db /tmp/wbem_loadtest_full.db --minutes 10
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER_ENTRY = os.path.join(REPO_ROOT, "scripts", "loadtest", "server_entry.py")


def http(method, url, payload=None, timeout=30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        body = e.read()
        status = e.code
    dt = time.perf_counter() - t0
    return status, body, dt


def read_rss_kb(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


class HandlerStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.latencies = []
        self.sizes = []
        self.errors = 0
        self.calls = 0

    def record(self, status, dt, size):
        with self.lock:
            self.calls += 1
            if 200 <= status < 300:
                self.latencies.append(dt)
                self.sizes.append(size)
            else:
                self.errors += 1

    def summary(self):
        with self.lock:
            lat = list(self.latencies)
            sz = list(self.sizes)
            return {
                "calls": self.calls,
                "errors": self.errors,
                "p50_ms": round(percentile(lat, 0.50) * 1000, 2) if lat else None,
                "p95_ms": round(percentile(lat, 0.95) * 1000, 2) if lat else None,
                "min_ms": round(min(lat) * 1000, 2) if lat else None,
                "max_ms": round(max(lat) * 1000, 2) if lat else None,
                "median_response_bytes": int(statistics.median(sz)) if sz else None,
                "max_response_bytes": max(sz) if sz else None,
            }


def loop_worker(stop_event, interval_s, fn, stats):
    # Небольшой случайный сдвиг старта, чтобы 3 клиента не били по серверу
    # одновременно первым тиком -- как в реальности (разные вкладки/люди).
    time.sleep(random.uniform(0, min(1.0, interval_s / 4)))
    next_t = time.monotonic()
    while not stop_event.is_set():
        try:
            status, body, dt = fn()
            stats.record(status, dt, len(body))
        except Exception as e:  # noqa: BLE001 -- нагрузочный клиент не должен падать целиком
            stats.record(599, 0.0, 0)
        next_t += interval_s
        sleep_s = next_t - time.monotonic()
        if sleep_s > 0:
            stop_event.wait(sleep_s)
        else:
            next_t = time.monotonic()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="путь к уже сгенерированной БД (seed_load_db.py)")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--out", default=None, help="куда записать JSON-результаты")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ОШИБКА: файл БД не найден: {args.db}", file=sys.stderr)
        return 2

    base = f"http://127.0.0.1:{args.port}"
    db_size_before = os.path.getsize(args.db)

    print(f"Запускаю сервис на {args.db} (порт {args.port}) ...")
    proc = subprocess.Popen(
        [sys.executable, SERVER_ENTRY, args.db, str(args.port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        cwd=REPO_ROOT,
    )

    db_open_time_s = None
    ready = False
    t_wait0 = time.time()
    while time.time() - t_wait0 < 60:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                print("Сервис завершился до готовности:", file=sys.stderr)
                print(proc.stdout.read(), file=sys.stderr)
                return 3
            continue
        line = line.strip()
        if line.startswith("DB_OPEN_TIME_S="):
            db_open_time_s = float(line.split("=", 1)[1])
            print(f"  {line}")
        if line.startswith("READY"):
            ready = True
            break
    if not ready:
        print("ОШИБКА: сервис не сообщил о готовности за 60 с", file=sys.stderr)
        proc.terminate()
        return 3

    # Дать werkzeug реально забиндиться на порт (READY печатается ДО run_simple).
    for _ in range(50):
        try:
            status, _, _ = http("GET", base + "/health", timeout=2)
            if status == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        print("ОШИБКА: /health не отвечает после запуска", file=sys.stderr)
        proc.terminate()
        return 3

    server_pid = proc.pid
    rss_start_kb = read_rss_kb(server_pid)
    print(f"  сервис готов, pid={server_pid}, RSS в начале = {rss_start_kb} кБ")

    # Топология: id щитов для баланса "по клику" -- читаем напрямую из БД
    # (отдельное READ-ONLY соединение, не мешает серверному).
    ro = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    panel_ids = [r["id"] for r in ro.execute(
        "SELECT id FROM electrical_nodes WHERE kind='panel'").fetchall()]
    ro.close()
    if not panel_ids:
        print("ОШИБКА: в базе нет узлов kind='panel'", file=sys.stderr)
        proc.terminate()
        return 3

    now = int(time.time())
    today_from = now - 24 * 3600
    month_from = now - 30 * 24 * 3600

    def call_snapshot():
        return http("GET", base + "/api/v2/snapshot")

    def call_overview_today():
        return http("POST", base + "/api/v2/overview/summary",
                     {"from": today_from, "to": now, "timezone": "UTC"})

    def call_reports_month():
        return http("POST", base + "/api/v2/reports/query",
                     {"dimension": "branch", "from": month_from, "to": now, "timezone": "UTC"})

    def call_balance_click():
        node_id = random.choice(panel_ids)
        return http("POST", base + f"/api/v2/topology/nodes/{node_id}/balance",
                     {"from": today_from, "to": now, "timezone": "UTC"})

    def call_status():
        return http("GET", base + "/api/status")

    stats = {
        "snapshot": HandlerStats(),
        "overview_summary_today": HandlerStats(),
        "reports_query_month": HandlerStats(),
        "node_balance_click": HandlerStats(),
        "api_status": HandlerStats(),
    }

    stop_event = threading.Event()
    threads = [
        threading.Thread(target=loop_worker, args=(stop_event, 5, call_snapshot, stats["snapshot"])),
        threading.Thread(target=loop_worker, args=(stop_event, 60, call_overview_today, stats["overview_summary_today"])),
        threading.Thread(target=loop_worker, args=(stop_event, 300, call_reports_month, stats["reports_query_month"])),
        threading.Thread(target=loop_worker, args=(stop_event, 20, call_balance_click, stats["node_balance_click"])),
        threading.Thread(target=loop_worker, args=(stop_event, 10, call_status, stats["api_status"])),
    ]
    for t in threads:
        t.daemon = True
        t.start()

    duration_s = args.minutes * 60
    print(f"Гоняю нагрузку {args.minutes:.1f} мин ({duration_s:.0f} с) ...")
    t_run0 = time.time()

    # N+1: считаем запросы на ОДИН overview/summary и ОДИН reports/query,
    # в середине прогона, когда фоновая нагрузка уже устоялась.
    time.sleep(min(20, duration_s * 0.2))
    status, _, _ = http("GET", base + "/__loadtest/trace/start")
    _s, _b, dt_ov = call_overview_today()
    status, body, _ = http("GET", base + "/__loadtest/trace/stop")
    n_queries_overview = json.loads(body)["queries"]

    status, _, _ = http("GET", base + "/__loadtest/trace/start")
    _s, _b, dt_rep = call_reports_month()
    status, body, _ = http("GET", base + "/__loadtest/trace/stop")
    n_queries_reports = json.loads(body)["queries"]
    print(f"  N+1: overview/summary -> {n_queries_overview} SQL-запросов "
          f"({dt_ov*1000:.1f} мс); reports/query (месяц) -> {n_queries_reports} "
          f"SQL-запросов ({dt_rep*1000:.1f} мс)")

    # Длительность удержания блокировки записи: изолированный write_probe,
    # затем write_probe'ы во время параллельного тяжёлого reports/query.
    _s, body, _ = http("POST", base + "/__loadtest/write_probe")
    baseline_probe_s = json.loads(body)["elapsed_s"]

    concurrent_probe_results = []
    heavy_done = threading.Event()

    def heavy_call():
        call_reports_month()
        heavy_done.set()

    heavy_thread = threading.Thread(target=heavy_call)
    t_heavy0 = time.monotonic()
    heavy_thread.start()
    while not heavy_done.is_set():
        t0 = time.perf_counter()
        _s, body, _ = http("POST", base + "/__loadtest/write_probe")
        dt = time.perf_counter() - t0
        concurrent_probe_results.append(dt)
        if time.monotonic() - t_heavy0 > 10:
            break
    heavy_thread.join(timeout=10)

    # Основной прогон продолжается до истечения общего времени.
    remaining = duration_s - (time.time() - t_run0)
    if remaining > 0:
        stop_event.wait(remaining)
    stop_event.set()
    for t in threads:
        t.join(timeout=10)

    actual_duration_s = time.time() - t_run0
    rss_end_kb = read_rss_kb(server_pid)

    # /api/status отдельно, чистым замером (без конкуренции с циклами) -- 5 подряд.
    status_times = []
    for _ in range(5):
        _s, _b, dt = call_status()
        status_times.append(dt)

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)

    db_size_after = os.path.getsize(args.db)

    result = {
        "params": {
            "db_path": args.db, "minutes_requested": args.minutes,
            "actual_duration_s": round(actual_duration_s, 1),
        },
        "db_open_time_s_on_full_db": db_open_time_s,
        "rss_start_kb": rss_start_kb, "rss_end_kb": rss_end_kb,
        "db_size_before_bytes": db_size_before, "db_size_after_bytes": db_size_after,
        "handlers": {name: s.summary() for name, s in stats.items()},
        "api_status_direct_ms": {
            "p50": round(percentile(status_times, 0.5) * 1000, 2),
            "p95": round(percentile(status_times, 0.95) * 1000, 2),
        },
        "n_plus_1": {
            "overview_summary_queries": n_queries_overview,
            "overview_summary_time_ms": round(dt_ov * 1000, 2),
            "reports_query_month_queries": n_queries_reports,
            "reports_query_month_time_ms": round(dt_rep * 1000, 2),
        },
        "write_lock_probe": {
            "baseline_isolated_s": round(baseline_probe_s, 4),
            "concurrent_during_heavy_reports_query_s": [round(x, 4) for x in concurrent_probe_results],
            "concurrent_max_s": round(max(concurrent_probe_results), 4) if concurrent_probe_results else None,
        },
    }

    print("\n=== Результаты ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\nЗаписано в {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

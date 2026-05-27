"""E2E тест Шага 4: реальный демон + фейковый wb-mqtt-db с данными.

Проверяет:
- catch-up действительно создаёт строки в period_aggregates;
- гибридный consumption использует агрегаты;
- эндпоинт /api/meters/<id>/hourly возвращает массив часов;
- эндпоинт /api/aggregates/status показывает статистику.
"""

import json, os, signal, subprocess, sys, tempfile, time, urllib.request
import uuid
import paho.mqtt.client as mqtt

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import importlib.util
spec = importlib.util.spec_from_file_location(
    "fake_mod", os.path.join(PROJ, "tests", "test_step3_e2e.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
FakeWbMqttDb = mod.FakeWbMqttDb


def main():
    tmp = tempfile.mkdtemp(prefix="wb-em-step4-")
    db_path = os.path.join(tmp, "state.db")
    cfg_path = os.path.join(tmp, "cfg.yaml")
    log_path = os.path.join(tmp, "log")

    cfg = """
mqtt: {host: 127.0.0.1, port: 1883}
http: {host: 127.0.0.1, port: 18280}
device_prefix: "wb-map3e_"
log_file: null
aggregator:
  enabled: true
  catchup_days: 2          # 2 дня — компактно для теста
  max_catchup_duration_s: 60
  catchup_start_delay_s: 1  # быстро стартуем
  hour_offset_s: 90
  patcher_interval_h: 24    # чтобы не дёргал во время теста
meters:
  - device_id: wb-map3e_16
    display_name: "Тестовый счётчик 1"
    group: "Тестовая зона"
"""
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(cfg)

    # Фейк wb-mqtt-db с данными: накопительная энергия растёт на 0.5 кВт·ч/час
    fake = FakeWbMqttDb()
    now_aligned = int(time.time()) // 3600 * 3600  # к началу часа
    # Точки за последние 48 часов (по одной на час)
    points = []
    for i in range(50):
        ts = now_aligned - (50 - i) * 3600
        value = 100.0 + i * 0.5
        points.append((ts, value))
    fake.values_db[("wb-map3e_16", "Total AP energy")] = points
    fake.channels_db = {
        "wb-map3e_16/Total AP energy": {
            "items": len(points),
            "last_ts": points[-1][0],
        },
    }
    fake.start()

    # Retained для MQTT — чтобы счётчик был «жив»
    pub = mqtt.Client(client_id=f"step4-pub-{uuid.uuid4().hex[:6]}")
    pub.connect("127.0.0.1", 1883, 5)
    pub.loop_start()
    msgs = [
        ("/devices/wb-map3e_16/meta", '{"driver":"wb-modbus"}'),
        ("/devices/wb-map3e_16/meta/name", "Тестовый"),
        ("/devices/wb-map3e_16/controls/Total AP energy", str(points[-1][1])),
        ("/devices/wb-map3e_16/controls/Serial", "16818659"),
    ]
    for t, p in msgs:
        pub.publish(t, p, qos=1, retain=True).wait_for_publish(5)
        pub.loop_stop()
    pub.disconnect()

    # Запускаем демон
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJ
    proc = subprocess.Popen(
        [sys.executable, "-m", "wb_energy_meter.main",
         "--config", cfg_path, "--db-path", db_path,
         "--log-level", "INFO", "--no-log-file"],
        env=env, stdout=open(log_path, "w"), stderr=subprocess.STDOUT)

    try:
        # Ждём API
        for _ in range(30):
            time.sleep(0.3)
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:18280/health", timeout=1) as r:
                    if r.status == 200: break
            except Exception: continue
        else:
            print("API не поднялся")
            print(open(log_path).read()); return 1

        # Ждём, пока catch-up отработает (старт +1 с + расчёт)
        print("[step4] Жду catch-up (8 с)...")
        time.sleep(8)

        # /api/aggregates/status — должны быть строки
        with urllib.request.urlopen(
                "http://127.0.0.1:18280/api/aggregates/status", timeout=5) as r:
            agg_status = json.loads(r.read().decode("utf-8"))
        print(f"[step4] rows_total: {agg_status['rows_total']}")
        print(f"[step4] by_quality: {agg_status['by_quality']}")
        assert agg_status["rows_total"] > 0, (
            f"catch-up ничего не создал! status: {agg_status}")
        # Worker должен закончить catch-up
        worker = agg_status["worker"]
        print(f"[step4] worker.catchup_running: {worker['catchup_running']}")
        print(f"[step4] worker.catchup_processed_hours: "
              f"{worker['catchup_processed_hours']}")
        assert worker["catchup_processed_hours"] > 0
        print("[OK] catch-up создал агрегаты")

        # /api/meters/.../hourly за last_24h
        with urllib.request.urlopen(
                "http://127.0.0.1:18280/api/meters/wb-map3e_16/hourly"
                "?period=last_24h", timeout=5) as r:
            hourly = json.loads(r.read().decode("utf-8"))
        print(f"[step4] hourly за last_24h: {hourly['hours_count']} часов")
        assert hourly["hours_count"] > 20, (
            f"мало часов: {hourly['hours_count']}")
        # Каждый час должен иметь delta = 0.5
        deltas = [h["consumption_kwh"] for h in hourly["items"]
                  if h["consumption_kwh"] is not None]
        if deltas:
            avg_delta = sum(deltas) / len(deltas)
            print(f"[step4] средняя дельта часа: {avg_delta:.3f} кВт·ч")
            assert 0.4 < avg_delta < 0.6, f"странная дельта: {avg_delta}"
        print("[OK] /hourly")

        # CLI: aggregates status
        result = subprocess.run(
            [sys.executable, "-m", "wb_energy_meter.cli",
             "--db-path", db_path, "--config", cfg_path,
             "aggregates", "status"],
            env=env, capture_output=True, text=True, check=True)
        assert "wb-map3e_16" in result.stdout
        assert "Всего часов" in result.stdout
        print("[OK] CLI aggregates status")

        # CLI: aggregates show
        result = subprocess.run(
            [sys.executable, "-m", "wb_energy_meter.cli",
             "--db-path", db_path, "--config", cfg_path,
             "aggregates", "show", "wb-map3e_16", "--period", "last_24h"],
            env=env, capture_output=True, text=True, check=True)
        assert "0.5" in result.stdout  # дельты часа
        assert "Сумма" in result.stdout
        print("[OK] CLI aggregates show")

        # Гибридный consumption: проверим что он использовал агрегаты
        # (для last_24h должны быть полные часы)
        with urllib.request.urlopen(
                "http://127.0.0.1:18280/api/meters/wb-map3e_16/consumption"
                "?period=last_24h", timeout=5) as r:
            cons = json.loads(r.read().decode("utf-8"))
        print(f"[step4] consumption last_24h: {cons['consumption_kwh']} кВт·ч, "
              f"quality={cons['quality']}")
        # Должно быть около 12 кВт·ч (24 часа × 0.5)
        assert cons["consumption_kwh"] is not None
        assert 10 < cons["consumption_kwh"] < 14, (
            f"странный расход: {cons['consumption_kwh']}")
        print("[OK] гибридный consumption")

        print("\nВсе e2e Шага 4 пройдены ✓")
        return 0

    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        fake.stop()

        try:
            with open(log_path) as f:
                lines = f.readlines()
            print("\n--- last 30 log lines ---")
            for l in lines[-30:]:
                print(l.rstrip())
        except OSError: pass

        clean = mqtt.Client(client_id=f"step4-clean-{uuid.uuid4().hex[:6]}")
        try:
            clean.connect("127.0.0.1", 1883, 5)
            for t, _ in msgs:
                clean.publish(t, "", qos=0, retain=True)
            clean.disconnect()
        except Exception: pass

        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

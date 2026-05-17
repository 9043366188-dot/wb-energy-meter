"""Полный e2e тест демона с новыми эндпоинтами Шага 3."""

import json, os, signal, subprocess, sys, tempfile, time, urllib.request
import threading, uuid
import paho.mqtt.client as mqtt

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

# Reuse fake from previous test
import importlib.util
spec = importlib.util.spec_from_file_location(
    "test_e2e_fake", os.path.join(PROJ, "tests", "test_step3_e2e.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
FakeWbMqttDb = mod.FakeWbMqttDb


def main():
    tmp = tempfile.mkdtemp(prefix="wb-em-e2e-")
    db_path = os.path.join(tmp, "state.db")
    cfg_path = os.path.join(tmp, "cfg.yaml")
    log_path = os.path.join(tmp, "log")

    cfg = """
mqtt: {host: 127.0.0.1, port: 1883}
http: {host: 127.0.0.1, port: 18180}
device_prefix: "wb-map3e_"
log_file: null
meters:
  - device_id: wb-map3e_16
    display_name: "Тестовый счётчик 1"
    group: "Тестовая зона"
"""
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(cfg)

    # Поднимаем фейковый wb-mqtt-db
    fake = FakeWbMqttDb()
    fake.channels_db = {
        "wb-map3e_16/Total AP energy": {"items": 5, "last_ts": 1700003600},
        "wb-map3e_16/Total P":         {"items": 5, "last_ts": 1700003600},
        "metrics/cpu":                 {"items": 100, "last_ts": 1700000000},
    }
    fake.values_db[("wb-map3e_16", "Total AP energy")] = [
        (1700000000.0, 100.0),
        (1700001800.0, 105.0),
        (1700003600.0, 110.0),
    ]
    fake.start()

    # Заливаем retained-снимок счётчика, чтобы демон его увидел
    pub = mqtt.Client(client_id=f"e2e-pub-{uuid.uuid4().hex[:6]}")
    pub.connect("127.0.0.1", 1883, 5)
    msgs = [
        ("/devices/wb-map3e_16/meta",
         '{"driver":"wb-modbus","title":{"en":"Тестовый"}}'),
        ("/devices/wb-map3e_16/meta/name", "Тестовый счетчик 1"),
        ("/devices/wb-map3e_16/controls/Urms L1", "0"),
        ("/devices/wb-map3e_16/controls/Urms L2", "0"),
        ("/devices/wb-map3e_16/controls/Urms L3", "0"),
        ("/devices/wb-map3e_16/controls/Irms L1", "0"),
        ("/devices/wb-map3e_16/controls/Irms L2", "0"),
        ("/devices/wb-map3e_16/controls/Irms L3", "0"),
        ("/devices/wb-map3e_16/controls/Total P", "0"),
        ("/devices/wb-map3e_16/controls/Total AP energy", "110.0"),
        ("/devices/wb-map3e_16/controls/Frequency", "0"),
        ("/devices/wb-map3e_16/controls/Serial", "16818659"),
    ]
    for t, p in msgs:
        pub.publish(t, p, qos=1, retain=True).wait_for_publish(5)
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
        ok = False
        for _ in range(30):
            time.sleep(0.3)
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:18180/health", timeout=1) as r:
                    if r.status == 200:
                        ok = True; break
            except Exception: continue
        if not ok:
            print("API не поднялся")
            print(open(log_path).read())
            return 1

        time.sleep(2)  # MQTT retained

        # /api/status
        with urllib.request.urlopen(
                "http://127.0.0.1:18180/api/status", timeout=5) as r:
            status = json.loads(r.read().decode("utf-8"))
        assert status["version"] == "0.3.0", f"version: {status['version']}"
        assert status["meters_total"] == 1
        print("[OK] /api/status")

        # /api/meters/wb-map3e_16/history-info
        with urllib.request.urlopen(
                "http://127.0.0.1:18180/api/meters/wb-map3e_16/history-info",
                timeout=5) as r:
            info = json.loads(r.read().decode("utf-8"))
        assert info["channels_in_history"] == 2
        controls = {x["control"] for x in info["items"]}
        assert "Total AP energy" in controls
        print(f"[OK] history-info: {info['channels_in_history']} каналов")

        # /api/meters/.../consumption — custom period
        url = ("http://127.0.0.1:18180/api/meters/wb-map3e_16/consumption"
               "?from=1970-01-20%2008:13:20&to=1970-01-20%2009:13:20")
        # 1700000000 = Tue Nov 14 22:13:20 2023 UTC, но для теста лучше custom
        # Используем явные unix timestamps преобразованные в local datetime
        from datetime import datetime
        from_str = datetime.fromtimestamp(1700000000.0).strftime("%Y-%m-%d %H:%M:%S")
        to_str = datetime.fromtimestamp(1700003600.0).strftime("%Y-%m-%d %H:%M:%S")
        url = (f"http://127.0.0.1:18180/api/meters/wb-map3e_16/consumption"
               f"?from={from_str.replace(' ', '%20')}"
               f"&to={to_str.replace(' ', '%20')}")
        with urllib.request.urlopen(url, timeout=5) as r:
            cons = json.loads(r.read().decode("utf-8"))
        assert cons["consumption_kwh"] == 10.0, f"got {cons['consumption_kwh']}"
        assert cons["quality"] == "ok"
        print(f"[OK] consumption: {cons['consumption_kwh']} kWh, "
              f"quality={cons['quality']}")

        # /api/summary/consumption
        url = (f"http://127.0.0.1:18180/api/summary/consumption"
               f"?from={from_str.replace(' ', '%20')}"
               f"&to={to_str.replace(' ', '%20')}")
        with urllib.request.urlopen(url, timeout=5) as r:
            summary = json.loads(r.read().decode("utf-8"))
        assert summary["consumption_kwh_total"] == 10.0
        print(f"[OK] summary: total={summary['consumption_kwh_total']} kWh")

        # CLI: consumption
        result = subprocess.run(
            [sys.executable, "-m", "wb_energy_meter.cli",
             "--db-path", db_path, "--config", cfg_path,
             "consumption", "wb-map3e_16",
             "--from", from_str, "--to", to_str],
            env=env, capture_output=True, text=True, check=True)
        assert "10.0000" in result.stdout
        assert "Тестовый счётчик 1" in result.stdout
        print("[OK] CLI consumption")

        # CLI: history-info wb-map3e_16
        result = subprocess.run(
            [sys.executable, "-m", "wb_energy_meter.cli",
             "--db-path", db_path, "--config", cfg_path,
             "history-info", "wb-map3e_16"],
            env=env, capture_output=True, text=True, check=True)
        assert "Total AP energy" in result.stdout
        print("[OK] CLI history-info")

        # CLI: consumption-summary
        result = subprocess.run(
            [sys.executable, "-m", "wb_energy_meter.cli",
             "--db-path", db_path, "--config", cfg_path,
             "consumption-summary", "--from", from_str, "--to", to_str],
            env=env, capture_output=True, text=True, check=True)
        assert "10.0000" in result.stdout
        print("[OK] CLI consumption-summary")

        print("\nВСЁ ОК ✓")
        return 0
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        fake.stop()

        # Last 30 lines of log
        try:
            with open(log_path) as f:
                lines = f.readlines()
            print("\n--- last 30 log lines ---")
            for l in lines[-30:]:
                print(l.rstrip())
        except OSError: pass

        # cleanup retained
        clean = mqtt.Client(client_id=f"e2e-clean-{uuid.uuid4().hex[:6]}")
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

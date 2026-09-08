"""Тесты Шага 10: канал Uptime и правка /etc/wb-mqtt-serial.conf (ТЗ v0.10.0).

Самостоятельный скрипт (не pytest), без root и без systemd, всё на
временных файлах:

    python tests/test_step10_wbserial.py

Главное здесь — не «фичи работают», а «при любом отказе чужой конфиг
остался байт в байт прежним». Этому посвящён отдельный блок в конце.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter import wb_serial_config as ws
from wb_energy_meter.api import PENDING_RESTART_KEY, _AppState, create_app
from wb_energy_meter.config import WbSerialConfigYaml, load_config


# ---------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------

SAMPLE_CONFIG = {
    "debug": False,
    "ports": [
        {
            "path": "/dev/ttyRS485-1",
            "baud_rate": 9600,
            "devices": [
                {
                    "device_type": "WB-MR6C",
                    "slave_id": "5",
                    "channels": [
                        {"name": "Relay 1", "enabled": True},
                    ],
                },
                {
                    "device_type": "WB-MAP3E",
                    "slave_id": "21",
                    "channels": [
                        {"name": "Total P", "enabled": True},
                        {"name": "Uptime", "enabled": False},
                    ],
                },
            ],
        },
        {
            "path": "/dev/ttyRS485-2",
            "devices": [
                {
                    "id": "щит-подвал",
                    "device_type": "WB-MAP3E",
                    "slave_id": "7",
                    "channels": [
                        {"name": "Total P", "enabled": True},
                    ],
                },
            ],
        },
    ],
}

TEMPLATE_MAP3E = {
    "device_type": "WB-MAP3E",
    "device": {"name": "WB-MAP3E", "id": "wb-map3e", "protocol": "modbus"},
}
TEMPLATE_MR6C = {
    "device_type": "WB-MR6C",
    "device": {"name": "WB-MR6C", "id": "wb-mr6c", "protocol": "modbus"},
}


def sha_of(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write_config(path, data=None, text=None):
    with open(path, "w", encoding="utf-8") as f:
        if text is not None:
            f.write(text)
        else:
            json.dump(data if data is not None else SAMPLE_CONFIG, f,
                      indent=4, ensure_ascii=False)


def make_templates(dirpath):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, "map3e.json"), "w", encoding="utf-8") as f:
        json.dump(TEMPLATE_MAP3E, f)
    with open(os.path.join(dirpath, "mr6c.json"), "w", encoding="utf-8") as f:
        json.dump(TEMPLATE_MR6C, f)
    with open(os.path.join(dirpath, "broken.json"), "w", encoding="utf-8") as f:
        f.write("{ это не json, // и вообще с комментарием")
    return dirpath


class FakeControl:
    def __init__(self, update_count=0, last_update_ts=0.0):
        self.update_count = update_count
        self.last_update_ts = last_update_ts


class FakeMeter:
    def __init__(self, device_id, controls=None, display_name=None):
        self.device_id = device_id
        self.controls = controls or {}
        self.effective_name = display_name or device_id


class FakeRegistry:
    def __init__(self, meters=None):
        self._m = {m.device_id: m for m in (meters or [])}

    def all(self):
        return list(self._m.values())

    def get(self, did):
        return self._m.get(did)


class FakeKv:
    """Имитация KvRepo поверх словаря — переживает «пересоздание
    приложения» ровно так же, как таблица kv переживает перезапуск
    сервиса."""
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


def make_app(config_path, templates_dir, allow_edit=False, backup_dir=None,
             meters=None, kv=None, restarter=None, service_name="fake-driver"):
    cfg = WbSerialConfigYaml(
        config_path=config_path, templates_dirs=[templates_dir],
        allow_edit=allow_edit,
        backup_dir=backup_dir or os.path.join(
            os.path.dirname(config_path), "backups"),
        service_name=service_name)
    state = _AppState(
        registry=FakeRegistry(meters or []), meters_repo=None,
        is_mqtt_connected=lambda: True,
        mqtt_message_count=lambda: 0, mqtt_error_count=lambda: 0,
        wb_db_client=None, consumption_service=None, started_at=0,
        wb_serial_config=cfg, kv_repo=kv, service_restarter=restarter)
    return create_app(state).test_client(), cfg


# ---------------------------------------------------------------------
# 1. find_device
# ---------------------------------------------------------------------

def test_find_device_by_explicit_id():
    found = ws.find_device(SAMPLE_CONFIG, "щит-подвал", {})
    assert found is not None
    pi, di, dev = found
    assert (pi, di) == (1, 0)
    assert dev["slave_id"] == "7"
    print("[OK] find_device: явный id")


def test_find_device_by_computed_id():
    id_map = {"WB-MAP3E": "wb-map3e", "WB-MR6C": "wb-mr6c"}
    found = ws.find_device(SAMPLE_CONFIG, "wb-map3e_21", id_map)
    assert found is not None
    pi, di, dev = found
    assert (pi, di) == (0, 1)
    assert dev["slave_id"] == "21"
    # Устройство с явным id не должно матчиться по вычисленному правилу
    assert ws.find_device(SAMPLE_CONFIG, "wb-map3e_7", id_map) is None
    print("[OK] find_device: вычисленный <prefix>_<slave_id>")


def test_find_device_not_found():
    id_map = {"WB-MAP3E": "wb-map3e"}
    assert ws.find_device(SAMPLE_CONFIG, "wb-map3e_99", id_map) is None
    assert ws.find_device(SAMPLE_CONFIG, "", id_map) is None
    print("[OK] find_device: не найдено -> None")


def test_find_device_ambiguous_refuses():
    """Найдено дважды — отказ. Лучше попросить человека, чем
    отредактировать чужое устройство."""
    data = copy.deepcopy(SAMPLE_CONFIG)
    # Два устройства с одинаковым явным id
    data["ports"][0]["devices"].append(
        {"id": "щит-подвал", "device_type": "WB-MAP3E", "slave_id": "31"})
    assert ws.find_device(data, "щит-подвал", {}) is None

    # Два устройства, дающие одинаковый вычисленный id
    data2 = copy.deepcopy(SAMPLE_CONFIG)
    data2["ports"][1]["devices"].append(
        {"device_type": "WB-MAP3E", "slave_id": "21"})
    assert ws.find_device(data2, "wb-map3e_21",
                          {"WB-MAP3E": "wb-map3e"}) is None
    print("[OK] find_device: найдено дважды -> отказ (None)")


def test_build_template_id_map():
    with tempfile.TemporaryDirectory() as d:
        tdir = make_templates(os.path.join(d, "templates"))
        id_map = ws.build_template_id_map([tdir, os.path.join(d, "нет-такого")])
        assert id_map["WB-MAP3E"] == "wb-map3e"
        assert id_map["WB-MR6C"] == "wb-mr6c"
        assert len(id_map) == 2, "битый шаблон не должен попадать в карту"
        print("[OK] build_template_id_map: битые файлы и отсутствующие "
              "каталоги молча пропускаются")


# ---------------------------------------------------------------------
# 2. uptime_channel_state
# ---------------------------------------------------------------------

def test_uptime_channel_state_all_three():
    assert ws.uptime_channel_state(
        {"channels": [{"name": "Uptime", "enabled": True}]}) == "enabled"
    assert ws.uptime_channel_state(
        {"channels": [{"name": "Uptime", "enabled": False}]}) == "disabled"
    assert ws.uptime_channel_state(
        {"channels": [{"name": "Total P"}]}) == "absent"
    assert ws.uptime_channel_state({}) == "absent"
    # Без ключа enabled драйвер считает канал включённым (README)
    assert ws.uptime_channel_state(
        {"channels": [{"name": "Uptime"}]}) == "enabled"
    print("[OK] uptime_channel_state: enabled / disabled / absent")


# ---------------------------------------------------------------------
# 3. set_uptime_enabled — точечность
# ---------------------------------------------------------------------

def test_set_uptime_enabled_updates_existing():
    data = copy.deepcopy(SAMPLE_CONFIG)
    new = ws.set_uptime_enabled(data, 0, 1)
    dev = new["ports"][0]["devices"][1]
    assert ws.uptime_channel_state(dev) == "enabled"
    # Исходный документ не мутирован
    assert ws.uptime_channel_state(
        data["ports"][0]["devices"][1]) == "disabled"
    print("[OK] set_uptime_enabled: обновление существующей записи")


def test_set_uptime_enabled_adds_new():
    data = copy.deepcopy(SAMPLE_CONFIG)
    new = ws.set_uptime_enabled(data, 1, 0)
    dev = new["ports"][1]["devices"][0]
    assert dev["channels"][-1] == {"name": "Uptime", "enabled": True}
    assert dev["channels"][0]["name"] == "Total P", "существующие каналы целы"
    print("[OK] set_uptime_enabled: добавление новой записи")


def test_set_uptime_enabled_adds_channels_key_when_missing():
    data = {"ports": [{"devices": [{"device_type": "X", "slave_id": "1"}]}]}
    new = ws.set_uptime_enabled(data, 0, 0)
    assert new["ports"][0]["devices"][0]["channels"] == [
        {"name": "Uptime", "enabled": True}]
    print("[OK] set_uptime_enabled: массив channels создаётся, если его не было")


def test_set_uptime_enabled_neighbours_byte_identical():
    """Соседние устройства и порты остались байт в байт теми же."""
    data = copy.deepcopy(SAMPLE_CONFIG)
    before = json.dumps(data, indent=4, ensure_ascii=False, sort_keys=False)
    new = ws.set_uptime_enabled(data, 0, 1)

    def branches(doc):
        return {
            "top_keys": list(doc.keys()),
            "debug": json.dumps(doc["debug"]),
            "port0_meta": json.dumps(
                {k: v for k, v in doc["ports"][0].items() if k != "devices"},
                ensure_ascii=False),
            "port0_dev0": json.dumps(doc["ports"][0]["devices"][0],
                                     ensure_ascii=False),
            "port1": json.dumps(doc["ports"][1], ensure_ascii=False),
            "touched_dev_except_channels": json.dumps(
                {k: v for k, v in doc["ports"][0]["devices"][1].items()
                 if k != "channels"}, ensure_ascii=False),
            "touched_dev_other_channels": json.dumps(
                [c for c in doc["ports"][0]["devices"][1]["channels"]
                 if c.get("name") != "Uptime"], ensure_ascii=False),
        }

    assert branches(data) == branches(new), "изменилось что-то помимо Uptime"
    # И сам исходный документ не тронут
    assert json.dumps(data, indent=4, ensure_ascii=False) == before
    print("[OK] set_uptime_enabled: остальные ветви документа байт в байт те же")


# ---------------------------------------------------------------------
# 4-8. save_config: JSON, sha256, бэкапы, атомарность, права
# ---------------------------------------------------------------------

def test_load_config_with_comments_refuses():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        write_config(path, text='{\n  // комментарий\n  "ports": []\n}\n')
        before = sha_of(path)
        try:
            ws.load_config(path)
            assert False, "должен был отказаться на не-JSON"
        except ws.WbSerialConfigError as e:
            assert "JSON" in str(e)
        assert sha_of(path) == before, "файл не должен был меняться"
        print("[OK] конфиг с комментариями // -> WbSerialConfigError, "
              "файл не изменён")


def test_load_config_missing_and_bad_root():
    with tempfile.TemporaryDirectory() as d:
        try:
            ws.load_config(os.path.join(d, "нет-файла.conf"))
            assert False
        except ws.WbSerialConfigError as e:
            assert "не найден" in str(e)
        path = os.path.join(d, "arr.conf")
        write_config(path, text="[1, 2, 3]")
        try:
            ws.load_config(path)
            assert False
        except ws.WbSerialConfigError as e:
            assert "корень" in str(e)
        print("[OK] load_config: нет файла / корень не объект -> внятный отказ")


def test_save_config_sha_mismatch_refuses():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        backups = os.path.join(d, "backups")
        write_config(path)
        before = sha_of(path)
        new_data = ws.set_uptime_enabled(copy.deepcopy(SAMPLE_CONFIG), 0, 1)
        try:
            ws.save_config(path, new_data, expected_sha="0" * 64,
                           backup_dir=backups)
            assert False, "должен был отказаться при несовпадении sha256"
        except ws.WbSerialConflict as e:
            assert "изменился" in str(e)
        assert sha_of(path) == before
        assert not os.path.exists(backups), \
            "бэкап не должен создаваться, если до записи дело не дошло"
        print("[OK] несовпадение sha256 -> отказ, файл не изменён, "
              "бэкап не создан")


def test_save_config_happy_path_and_formatting():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        backups = os.path.join(d, "backups")
        data = copy.deepcopy(SAMPLE_CONFIG)
        data["ports"][0]["devices"][1]["name"] = "Ввод №1 (щит)"
        write_config(path, data)
        _, sha = ws.load_config(path)
        new_data = ws.set_uptime_enabled(data, 0, 1)
        res = ws.save_config(path, new_data, expected_sha=sha,
                             backup_dir=backups)
        assert os.path.exists(res["backup_path"])
        got, new_sha = ws.load_config(path)
        assert new_sha == res["sha256"]
        assert ws.uptime_channel_state(
            got["ports"][0]["devices"][1]) == "enabled"
        with open(path, encoding="utf-8") as f:
            text = f.read()
        assert "Ввод №1 (щит)" in text, "ensure_ascii=False не соблюдён"
        assert "\\u" not in text
        assert '\n    "ports"' in text, "indent=4 не соблюдён"
        print("[OK] запись: indent=4, ensure_ascii=False, изменение на месте")


def test_backups_rotate_keeping_ten():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        backups = os.path.join(d, "backups")
        write_config(path)
        for i in range(13):
            _, sha = ws.load_config(path)
            data, _ = ws.load_config(path)
            data["debug"] = bool(i % 2)
            ws.save_config(path, data, expected_sha=sha, backup_dir=backups)
            # Метка времени в имени — секундная, разводим искусственно
            time.sleep(0.01)
        items = ws.list_backups(path, backups)
        assert len(items) == 10, "должно остаться ровно 10 бэкапов: %d" % len(items)
        print("[OK] бэкапы создаются, старше 10 удаляются")


def test_write_is_atomic_no_tmp_leftovers():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        backups = os.path.join(d, "backups")
        write_config(path)
        _, sha = ws.load_config(path)
        new_data = ws.set_uptime_enabled(copy.deepcopy(SAMPLE_CONFIG), 0, 1)
        ws.save_config(path, new_data, expected_sha=sha, backup_dir=backups)
        leftovers = [n for n in os.listdir(d)
                     if n not in ("wb-mqtt-serial.conf", "backups")]
        assert leftovers == [], "остались временные файлы: %s" % leftovers
        print("[OK] атомарность: .tmp-хвостов в каталоге не осталось")


def test_file_mode_preserved():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        backups = os.path.join(d, "backups")
        write_config(path)
        os.chmod(path, 0o640)
        mode_before = stat.S_IMODE(os.stat(path).st_mode)
        _, sha = ws.load_config(path)
        new_data = ws.set_uptime_enabled(copy.deepcopy(SAMPLE_CONFIG), 0, 1)
        ws.save_config(path, new_data, expected_sha=sha, backup_dir=backups)
        mode_after = stat.S_IMODE(os.stat(path).st_mode)
        assert mode_after == mode_before == 0o640, oct(mode_after)
        print("[OK] права файла сохраняются (0640 до и после записи)")


def test_verify_failure_rolls_back():
    """Проверка после записи не прошла -> откат из бэкапа (§5.6)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        backups = os.path.join(d, "backups")
        write_config(path)
        before = sha_of(path)
        _, sha = ws.load_config(path)
        new_data = ws.set_uptime_enabled(copy.deepcopy(SAMPLE_CONFIG), 0, 1)
        try:
            ws.save_config(path, new_data, expected_sha=sha,
                           backup_dir=backups, verify=lambda d2: False)
            assert False, "должен был упасть на проверке после записи"
        except ws.WbSerialConfigError as e:
            assert "восстановлен" in str(e)
        assert sha_of(path) == before, \
            "после неудачной проверки конфиг обязан быть откачен байт в байт"
        print("[OK] неудачная проверка после записи -> откат из бэкапа, "
              "файл байт в байт прежний")


def test_restore_backup_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        backups = os.path.join(d, "backups")
        write_config(path)
        original = sha_of(path)
        b = ws.make_backup(path, backups)
        _, sha = ws.load_config(path)
        ws.save_config(path, ws.set_uptime_enabled(
            copy.deepcopy(SAMPLE_CONFIG), 0, 1),
            expected_sha=sha, backup_dir=backups)
        assert sha_of(path) != original
        ws.restore_backup(b, path)
        assert sha_of(path) == original
        print("[OK] restore_backup возвращает файл байт в байт")


# ---------------------------------------------------------------------
# 9. API
# ---------------------------------------------------------------------

def test_api_enable_forbidden_when_allow_edit_false():
    """403 и — принципиально — файл вообще не открывался на запись."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        tdir = make_templates(os.path.join(d, "templates"))
        write_config(path)
        before_sha = sha_of(path)
        before_mtime = os.stat(path).st_mtime_ns
        client, cfg = make_app(path, tdir, allow_edit=False, kv=FakeKv())
        r = client.post("/api/wb-config/enable-uptime",
                        json={"device_id": "wb-map3e_21",
                              "sha256": before_sha})
        assert r.status_code == 403, r.status_code
        assert "allow_edit" in r.get_json()["error"]
        assert sha_of(path) == before_sha
        assert os.stat(path).st_mtime_ns == before_mtime, \
            "файл трогали, хотя правка запрещена"
        assert not os.path.exists(cfg.backup_dir), \
            "бэкап не должен создаваться при allow_edit: false"
        print("[OK] API: allow_edit=false -> 403, файл не открывался на запись")


def test_api_enable_sha_conflict_409():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        tdir = make_templates(os.path.join(d, "templates"))
        write_config(path)
        before = sha_of(path)
        client, _ = make_app(path, tdir, allow_edit=True, kv=FakeKv())
        r = client.post("/api/wb-config/enable-uptime",
                        json={"device_id": "wb-map3e_21", "sha256": "beef" * 16})
        assert r.status_code == 409
        assert "изменился" in r.get_json()["error"]
        assert sha_of(path) == before
        print("[OK] API: конфликт sha256 -> 409, файл не изменён")


def test_api_enable_device_not_found_409():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        tdir = make_templates(os.path.join(d, "templates"))
        write_config(path)
        before = sha_of(path)
        client, _ = make_app(path, tdir, allow_edit=True, kv=FakeKv())
        r = client.post("/api/wb-config/enable-uptime",
                        json={"device_id": "wb-map3e_99", "sha256": before})
        assert r.status_code == 409
        assert sha_of(path) == before
        print("[OK] API: устройство не найдено -> 409, файл не изменён")


def test_api_enable_success_and_pending_survives_app_restart():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        tdir = make_templates(os.path.join(d, "templates"))
        write_config(path)
        kv_store = {}
        client, _ = make_app(path, tdir, allow_edit=True,
                             kv=FakeKv(kv_store))
        sha = sha_of(path)
        r = client.post("/api/wb-config/enable-uptime",
                        json={"device_id": "wb-map3e_21", "sha256": sha})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["needs_restart"] is True
        assert body["pending"] == ["wb-map3e_21"]
        assert body["sha256"] != sha
        data, _ = ws.load_config(path)
        assert ws.uptime_channel_state(
            data["ports"][0]["devices"][1]) == "enabled"
        assert kv_store[PENDING_RESTART_KEY] == ["wb-map3e_21"]

        # Пересоздаём приложение — pending лежит в kv и обязан выжить
        client2, _ = make_app(path, tdir, allow_edit=True,
                              kv=FakeKv(kv_store))
        r2 = client2.get("/api/wb-config/pending")
        assert r2.get_json()["pending"] == ["wb-map3e_21"]
        assert r2.get_json()["needs_restart"] is True
        print("[OK] API: успешная правка -> 200, pending в kv переживает "
              "пересоздание приложения")


def test_api_enable_already_enabled_does_not_write():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        tdir = make_templates(os.path.join(d, "templates"))
        data = copy.deepcopy(SAMPLE_CONFIG)
        data["ports"][0]["devices"][1]["channels"][1]["enabled"] = True
        write_config(path, data)
        before = sha_of(path)
        client, _ = make_app(path, tdir, allow_edit=True, kv=FakeKv())
        r = client.post("/api/wb-config/enable-uptime",
                        json={"device_id": "wb-map3e_21", "sha256": before})
        assert r.status_code == 200
        assert r.get_json().get("already_enabled") is True
        assert sha_of(path) == before
        print("[OK] API: канал уже включён -> ничего не пишем")


def test_api_restart_forbidden_when_allow_edit_false():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        tdir = make_templates(os.path.join(d, "templates"))
        write_config(path)
        called = []
        client, _ = make_app(path, tdir, allow_edit=False, kv=FakeKv(),
                             restarter=lambda name: (called.append(name),
                                                     (True, "ok"))[1])
        r = client.post("/api/wb-config/restart-driver")
        assert r.status_code == 403
        assert called == [], "systemctl не должен вызываться при allow_edit=false"
        print("[OK] API: restart-driver при allow_edit=false -> 403, "
              "systemctl не вызван")


def test_api_restart_success_clears_pending():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        tdir = make_templates(os.path.join(d, "templates"))
        write_config(path)
        kv_store = {PENDING_RESTART_KEY: ["wb-map3e_21"]}
        client, _ = make_app(path, tdir, allow_edit=True, kv=FakeKv(kv_store),
                             restarter=lambda name: (True, "active"))
        r = client.post("/api/wb-config/restart-driver")
        assert r.status_code == 200
        assert r.get_json()["pending"] == []
        assert kv_store[PENDING_RESTART_KEY] == []
        print("[OK] API: успешный перезапуск очищает pending")


def test_api_restart_failure_rolls_back_config():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        tdir = make_templates(os.path.join(d, "templates"))
        backups = os.path.join(d, "backups")
        write_config(path)
        original_sha = sha_of(path)
        kv_store = {}
        client, _ = make_app(path, tdir, allow_edit=True, backup_dir=backups,
                             kv=FakeKv(kv_store),
                             restarter=lambda name: (False, "не поднялся"))
        # Сначала реально правим конфиг (создастся бэкап)
        r = client.post("/api/wb-config/enable-uptime",
                        json={"device_id": "wb-map3e_21",
                              "sha256": sha_of(path)})
        assert r.status_code == 200
        assert sha_of(path) != original_sha
        # Теперь перезапуск не удаётся — конфиг обязан откатиться
        r2 = client.post("/api/wb-config/restart-driver")
        assert r2.status_code == 500
        body = r2.get_json()
        assert body["restored"] is True
        assert body["backup_path"]
        assert sha_of(path) == original_sha, \
            "конфиг не откачен после неподнявшегося драйвера"
        print("[OK] API: драйвер не поднялся -> откат конфига из бэкапа, "
              "500 с путём к бэкапу")


def test_restart_service_rejects_weird_unit_name():
    ok, detail = ws.restart_service("wb-mqtt-serial; rm -rf /",
                                    runner=lambda *a, **k: None)
    assert ok is False and "Недопустимое имя" in detail
    print("[OK] restart_service: подозрительное имя юнита отвергается")


def test_restart_service_waits_for_active():
    calls = []

    class R:
        def __init__(self, rc, out=b""):
            self.returncode = rc
            self.stdout = out

    seq = [b"activating\n", b"activating\n", b"active\n"]

    def runner(cmd, timeout=None):
        calls.append(cmd)
        if cmd[1] == "restart":
            return R(0)
        return R(0, seq.pop(0) if seq else b"active\n")

    ok, detail = ws.restart_service("fake-driver", timeout_s=30,
                                    runner=runner, sleep=lambda s: None)
    assert ok is True and detail == "active"
    assert calls[0][:2] == ["systemctl", "restart"]
    print("[OK] restart_service дожидается is-active")


def test_restart_service_timeout():
    class R:
        returncode = 0
        stdout = b"failed\n"

    ok, detail = ws.restart_service(
        "fake-driver", timeout_s=0.01,
        runner=lambda cmd, timeout=None: R(), sleep=lambda s: None)
    assert ok is False and "не поднялся" in detail
    print("[OK] restart_service: таймаут -> честный отказ")


# ---------------------------------------------------------------------
# 10. Старый конфиг сервиса без секции wb_serial:
# ---------------------------------------------------------------------

def test_old_service_config_without_wb_serial_section():
    old_yaml = (
        "mqtt:\n"
        "  host: 127.0.0.1\n"
        "  port: 1883\n"
        "http:\n"
        "  port: 8080\n"
        "meters:\n"
        "  - device_id: wb-map3e_21\n"
        "    display_name: Ввод 1\n"
    )
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-energy-meter.conf")
        with open(path, "w", encoding="utf-8") as f:
            f.write(old_yaml)
        cfg = load_config(path)
        assert cfg.wb_serial.allow_edit is False
        assert cfg.wb_serial.config_path == "/etc/wb-mqtt-serial.conf"
        assert cfg.wb_serial.service_name == "wb-mqtt-serial"
        assert len(cfg.wb_serial.templates_dirs) == 2
        assert cfg.meters[0].device_id == "wb-map3e_21"
        print("[OK] старый конфиг без секции wb_serial: не ломает запуск, "
              "allow_edit по умолчанию False")


def test_new_service_config_with_wb_serial_section():
    yaml_text = (
        "mqtt:\n  host: 127.0.0.1\n"
        "wb_serial:\n"
        "  config_path: /tmp/x.conf\n"
        "  allow_edit: true\n"
        "  service_name: wb-mqtt-serial\n"
        "  backup_dir: /tmp/backups\n"
        "  templates_dirs:\n"
        "    - /tmp/templates\n"
    )
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-energy-meter.conf")
        with open(path, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        cfg = load_config(path)
        assert cfg.wb_serial.allow_edit is True
        assert cfg.wb_serial.config_path == "/tmp/x.conf"
        assert cfg.wb_serial.templates_dirs == ["/tmp/templates"]
        print("[OK] секция wb_serial: читается целиком")


# ---------------------------------------------------------------------
# 11. Диагностика: все шесть состояний
# ---------------------------------------------------------------------

def _meter_with_uptime(count, age_s):
    now = time.time()
    return FakeMeter("wb-map3e_21", {
        "Uptime": FakeControl(update_count=count,
                              last_update_ts=now - age_s if count else 0.0)})


def test_diagnostics_six_states():
    id_map = {"WB-MAP3E": "wb-map3e"}
    fresh = _meter_with_uptime(10, 5)
    silent = _meter_with_uptime(0, 0)

    data_enabled = copy.deepcopy(SAMPLE_CONFIG)
    data_enabled["ports"][0]["devices"][1]["channels"][1]["enabled"] = True
    data_absent = copy.deepcopy(SAMPLE_CONFIG)
    data_absent["ports"][0]["devices"][1]["channels"] = [
        {"name": "Total P", "enabled": True}]

    # ok
    d = ws.describe_meter(fresh, "wb-map3e_21", data_enabled, id_map)
    assert d["state"] == "ok", d
    # disabled
    d = ws.describe_meter(silent, "wb-map3e_21", SAMPLE_CONFIG, id_map)
    assert d["state"] == "disabled", d
    assert "in queue order" in d["detail"]
    # not_in_config
    d = ws.describe_meter(silent, "wb-map3e_21", data_absent, id_map)
    assert d["state"] == "not_in_config", d
    # stale
    d = ws.describe_meter(_meter_with_uptime(10, 600), "wb-map3e_21",
                          data_enabled, id_map)
    assert d["state"] == "stale", d
    # device_not_found
    d = ws.describe_meter(fresh, "wb-map3e_99", SAMPLE_CONFIG, id_map)
    assert d["state"] == "device_not_found", d
    # unknown
    d = ws.describe_meter(fresh, "wb-map3e_21", None, None,
                          config_error="нет файла")
    assert d["state"] == "unknown", d
    assert "нет файла" in d["detail"]
    print("[OK] диагностика: воспроизведены все шесть состояний §3.2")


def test_can_enable_only_when_allowed_found_and_off():
    id_map = {"WB-MAP3E": "wb-map3e"}
    silent = _meter_with_uptime(0, 0)
    assert ws.describe_meter(silent, "wb-map3e_21", SAMPLE_CONFIG, id_map,
                             allow_edit=False)["can_enable"] is False
    assert ws.describe_meter(silent, "wb-map3e_21", SAMPLE_CONFIG, id_map,
                             allow_edit=True)["can_enable"] is True
    assert ws.describe_meter(silent, "wb-map3e_99", SAMPLE_CONFIG, id_map,
                             allow_edit=True)["can_enable"] is False
    assert ws.describe_meter(_meter_with_uptime(10, 1), "wb-map3e_21",
                             SAMPLE_CONFIG, id_map,
                             allow_edit=True)["can_enable"] is False
    print("[OK] can_enable: только allow_edit + устройство найдено + канал off")


def test_api_diagnostics_never_500_without_config():
    """Конфига нет вовсе — статус unknown и спокойные 200, не 500."""
    with tempfile.TemporaryDirectory() as d:
        missing = os.path.join(d, "нет-такого.conf")
        tdir = os.path.join(d, "нет-шаблонов")
        client, _ = make_app(missing, tdir, allow_edit=False, kv=FakeKv(),
                             meters=[_meter_with_uptime(0, 0)])
        r = client.get("/api/meters/wb-map3e_21/uptime-channel")
        assert r.status_code == 200, r.status_code
        assert r.get_json()["state"] == "unknown"
        r2 = client.get("/api/uptime-channel/summary")
        assert r2.status_code == 200
        body = r2.get_json()
        assert body["items"][0]["state"] == "unknown"
        assert body["config_error"]
        assert body["allow_edit"] is False
        print("[OK] API: нечитаемый конфиг -> unknown и 200, никаких 500")


def test_api_summary_reads_config_once():
    """Конфиг читается один раз на весь ответ, а не по разу на счётчик."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "wb-mqtt-serial.conf")
        tdir = make_templates(os.path.join(d, "templates"))
        write_config(path)
        meters = [FakeMeter("wb-map3e_21"), FakeMeter("щит-подвал"),
                  FakeMeter("wb-map3e_99")]
        cfg = WbSerialConfigYaml(
            config_path=path, templates_dirs=[tdir], allow_edit=True,
            backup_dir=os.path.join(d, "backups"))

        calls = {"n": 0}
        real_load = ws.load_config

        class CountingWs:
            def __getattr__(self, item):
                return getattr(ws, item)

            def load_config(self, p):
                calls["n"] += 1
                return real_load(p)

        state = _AppState(
            registry=FakeRegistry(meters), meters_repo=None,
            is_mqtt_connected=lambda: True,
            mqtt_message_count=lambda: 0, mqtt_error_count=lambda: 0,
            wb_db_client=None, consumption_service=None, started_at=0,
            wb_serial_config=cfg, kv_repo=FakeKv(),
            wb_serial=CountingWs())
        client = create_app(state).test_client()
        r = client.get("/api/uptime-channel/summary")
        assert r.status_code == 200
        assert calls["n"] == 1, "конфиг прочитан %d раз(а)" % calls["n"]
        states = {i["device_id"]: i["state"] for i in r.get_json()["items"]}
        assert states["wb-map3e_99"] == "device_not_found"
        assert states["wb-map3e_21"] == "disabled"
        print("[OK] API: summary читает конфиг ровно один раз")


# ---------------------------------------------------------------------
# ОТДЕЛЬНО И ОБЯЗАТЕЛЬНО: при ЛЮБОМ отказе конфиг байт в байт прежний
# ---------------------------------------------------------------------

def test_config_byte_identical_on_every_failure_path():
    """Сравнение sha256 файла до и после для всех сценариев отказа:
    не JSON, конфликт sha256, устройство не найдено, найдено дважды,
    allow_edit: false. Это главный тест раздела 5 ТЗ."""
    scenarios = []

    def scenario(name, config_text, allow_edit, device_id, sha_mode,
                 expected_code):
        scenarios.append((name, config_text, allow_edit, device_id, sha_mode,
                          expected_code))

    good = json.dumps(SAMPLE_CONFIG, indent=4, ensure_ascii=False)
    dup = copy.deepcopy(SAMPLE_CONFIG)
    dup["ports"][1]["devices"].append(
        {"device_type": "WB-MAP3E", "slave_id": "21"})
    dup_text = json.dumps(dup, indent=4, ensure_ascii=False)

    scenario("не JSON (комментарии //)",
             '{\n  // тут комментарий\n  "ports": []\n}\n',
             True, "wb-map3e_21", "real", 409)
    scenario("несовпадение sha256", good, True, "wb-map3e_21", "wrong", 409)
    scenario("устройство не найдено", good, True, "wb-map3e_777", "real", 409)
    scenario("найдено дважды", dup_text, True, "wb-map3e_21", "real", 409)
    scenario("allow_edit: false", good, False, "wb-map3e_21", "real", 403)

    for (name, text, allow_edit, device_id, sha_mode,
         expected_code) in scenarios:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "wb-mqtt-serial.conf")
            tdir = make_templates(os.path.join(d, "templates"))
            backups = os.path.join(d, "backups")
            write_config(path, text=text)
            before_sha = sha_of(path)
            before_bytes = open(path, "rb").read()
            before_mtime = os.stat(path).st_mtime_ns

            client, _ = make_app(path, tdir, allow_edit=allow_edit,
                                 backup_dir=backups, kv=FakeKv())
            sha = before_sha if sha_mode == "real" else "dead" * 16
            r = client.post("/api/wb-config/enable-uptime",
                            json={"device_id": device_id, "sha256": sha})
            assert r.status_code == expected_code, \
                "%s: ожидали %d, получили %d (%s)" % (
                    name, expected_code, r.status_code, r.get_data(as_text=True))

            after_sha = sha_of(path)
            assert after_sha == before_sha, \
                "%s: конфиг ИЗМЕНИЛСЯ (sha %s -> %s)" % (
                    name, before_sha[:12], after_sha[:12])
            assert open(path, "rb").read() == before_bytes, \
                "%s: содержимое конфига изменилось байт в байт" % name
            assert os.stat(path).st_mtime_ns == before_mtime, \
                "%s: файл открывали на запись (mtime изменился)" % name
            assert not os.path.exists(backups) or not os.listdir(backups), \
                "%s: создан бэкап, хотя до записи дело не дошло" % name
            leftovers = [n for n in os.listdir(d)
                         if n not in ("wb-mqtt-serial.conf", "templates",
                                       "backups")]
            assert leftovers == [], "%s: мусор в каталоге: %s" % (name, leftovers)
            print("    [OK] %s -> %d, конфиг байт в байт прежний"
                  % (name, expected_code))
    print("[OK] при ЛЮБОМ сценарии отказа конфиг остался байт в байт прежним")


if __name__ == "__main__":
    test_find_device_by_explicit_id()
    test_find_device_by_computed_id()
    test_find_device_not_found()
    test_find_device_ambiguous_refuses()
    test_build_template_id_map()

    test_uptime_channel_state_all_three()

    test_set_uptime_enabled_updates_existing()
    test_set_uptime_enabled_adds_new()
    test_set_uptime_enabled_adds_channels_key_when_missing()
    test_set_uptime_enabled_neighbours_byte_identical()

    test_load_config_with_comments_refuses()
    test_load_config_missing_and_bad_root()
    test_save_config_sha_mismatch_refuses()
    test_save_config_happy_path_and_formatting()
    test_backups_rotate_keeping_ten()
    test_write_is_atomic_no_tmp_leftovers()
    test_file_mode_preserved()
    test_verify_failure_rolls_back()
    test_restore_backup_roundtrip()

    test_api_enable_forbidden_when_allow_edit_false()
    test_api_enable_sha_conflict_409()
    test_api_enable_device_not_found_409()
    test_api_enable_success_and_pending_survives_app_restart()
    test_api_enable_already_enabled_does_not_write()
    test_api_restart_forbidden_when_allow_edit_false()
    test_api_restart_success_clears_pending()
    test_api_restart_failure_rolls_back_config()
    test_restart_service_rejects_weird_unit_name()
    test_restart_service_waits_for_active()
    test_restart_service_timeout()

    test_old_service_config_without_wb_serial_section()
    test_new_service_config_with_wb_serial_section()

    test_diagnostics_six_states()
    test_can_enable_only_when_allowed_found_and_off()
    test_api_diagnostics_never_500_without_config()
    test_api_summary_reads_config_once()

    test_config_byte_identical_on_every_failure_path()

    print("\nВсе тесты Шага 10 (канал Uptime, wb-mqtt-serial.conf) пройдены.")

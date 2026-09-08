"""Тесты Шага 9: самообновление из GitHub (updater.py + API), ТЗ v0.9.0.

Самостоятельный скрипт (не pytest), без сети:
    python tests/test_step9_updater.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter.api import _AppState, create_app
from wb_energy_meter.updater import (
    ACTIVE_STATES, UpdateCheckError, UpdateInProgressError,
    is_update_available, parse_version_from_init, read_status,
    start_update, write_status,
)


# ---------------------------------------------------------------------
# 1. is_update_available
# ---------------------------------------------------------------------

def test_is_update_available_same_sha():
    installed = {"version": "0.8.0", "commit": "abc1234"}
    remote = {"version": "0.9.0", "commit": "abc1234"}
    assert is_update_available(installed, remote) is False
    print("[OK] совпадающие sha -> обновления нет")


def test_is_update_available_different_sha():
    installed = {"version": "0.8.0", "commit": "abc1234"}
    remote = {"version": "0.9.0", "commit": "def5678"}
    assert is_update_available(installed, remote) is True
    print("[OK] разные sha -> обновление доступно")


def test_is_update_available_no_installed_commit():
    installed = {"version": "0.8.0", "commit": None}
    remote = {"version": "0.9.0", "commit": "def5678"}
    assert is_update_available(installed, remote) is True
    print("[OK] commit=None у установленной версии -> обновление доступно")


# ---------------------------------------------------------------------
# 2. Парсинг __version__ из __init__.py
# ---------------------------------------------------------------------

def test_parse_version_double_quotes():
    assert parse_version_from_init('__version__ = "0.9.0"') == "0.9.0"


def test_parse_version_single_quotes():
    assert parse_version_from_init("__version__ = '0.9.0'") == "0.9.0"


def test_parse_version_extra_spaces():
    assert parse_version_from_init('__version__   =   "0.9.0"') == "0.9.0"
    assert parse_version_from_init('__version__="0.9.0"') == "0.9.0"


def test_parse_version_in_full_file_text():
    text = (
        '"""Docstring."""\n'
        '__version__ = "0.9.0"\n'
        '__app_name__ = "wb-energy-meter"\n'
    )
    assert parse_version_from_init(text) == "0.9.0"


def test_parse_version_missing():
    assert parse_version_from_init("no version here") is None
    assert parse_version_from_init("") is None
    assert parse_version_from_init(None) is None


print("[OK] парсинг __version__ — оба вида кавычек, лишние пробелы, отсутствие")


# ---------------------------------------------------------------------
# 3. read_status / write_status — атомарность
# ---------------------------------------------------------------------

def test_write_then_read_status_full():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "update-status.json")
        write_status(path, state="downloading", step_label="Скачивание…",
                     started_at=123, from_version="0.8.0")
        got = read_status(path)
        assert got["state"] == "downloading"
        assert got["step_label"] == "Скачивание…"
        assert got["started_at"] == 123
        assert got["from_version"] == "0.8.0"
        print("[OK] write_status/read_status: запись читается целиком")


def test_write_status_merges_fields():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "update-status.json")
        write_status(path, state="starting", step_label="a", from_version="0.8.0")
        write_status(path, state="downloading", step_label="b")
        got = read_status(path)
        # state/step_label обновились, from_version из первой записи сохранился
        assert got["state"] == "downloading"
        assert got["step_label"] == "b"
        assert got["from_version"] == "0.8.0"
        print("[OK] write_status сливает поля, не затирая документ целиком")


def test_read_status_missing_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "does-not-exist.json")
        got = read_status(path)
        assert got == {"state": "idle"}
        print("[OK] read_status без файла -> {'state': 'idle'}")


def test_read_status_broken_json():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "broken.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json,,,")
        got = read_status(path)
        assert got == {"state": "idle"}
        print("[OK] read_status с битым JSON -> {'state': 'idle'}, без исключения")


def test_write_status_is_atomic_no_tmp_leftover():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "update-status.json")
        write_status(path, state="success")
        # Никаких временных файлов не должно оставаться рядом
        leftovers = [f for f in os.listdir(d) if f != "update-status.json"]
        assert leftovers == [], f"остались временные файлы: {leftovers}"
        # Файл — валидный JSON целиком (не обрезан)
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        print("[OK] write_status атомарна: временный файл не остаётся, JSON не обрезан")


# ---------------------------------------------------------------------
# 4. API через app.test_client() с подменённым updater
# ---------------------------------------------------------------------

class FakeUpdateConfig:
    def __init__(self, enabled=True, allow_from_ui=True,
                 repo_owner="9043366188-dot", repo_name="wb-energy-meter",
                 ref="main", check_timeout_s=10):
        self.enabled = enabled
        self.allow_from_ui = allow_from_ui
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.ref = ref
        self.check_timeout_s = check_timeout_s


class FakeUpdater:
    """Подмена wb_energy_meter.updater для теста API — без сети и без
    реального запуска процессов."""
    UpdateCheckError = UpdateCheckError
    UpdateInProgressError = UpdateInProgressError
    ACTIVE_STATES = ACTIVE_STATES

    def __init__(self, status=None, remote=None, installed=None,
                 check_error=None):
        self.status = status if status is not None else {"state": "idle"}
        self.remote = remote if remote is not None else {
            "version": "0.9.0", "commit": "def5678",
            "message": "feat: пример", "date": "2026-09-08T10:00:00Z"}
        self.installed = installed if installed is not None else {
            "version": "0.8.0", "commit": "abc1234",
            "ref": "main", "installed_at": 1000}
        self.check_error = check_error
        self.check_remote_calls = 0
        self.start_update_calls = []

    def get_installed_info(self, install_dir):
        return self.installed

    def check_remote(self, owner, repo, ref, timeout=10):
        self.check_remote_calls += 1
        if self.check_error is not None:
            raise self.check_error
        return self.remote

    def is_update_available(self, installed, remote):
        return (installed or {}).get("commit") != (remote or {}).get("commit")

    def read_status(self, path):
        return self.status

    def start_update(self, **kwargs):
        self.start_update_calls.append(kwargs)
        return {"state": "starting"}


class FakeRegistry:
    def all(self): return []
    def get(self, d): return None


def make_client(update_config=None, fake_updater=None, status_path=None):
    state = _AppState(
        registry=FakeRegistry(), meters_repo=None,
        is_mqtt_connected=lambda: True,
        mqtt_message_count=lambda: 0, mqtt_error_count=lambda: 0,
        wb_db_client=None, consumption_service=None, started_at=0,
        aggregates_repo=None, aggregator=None,
        update_config=update_config or FakeUpdateConfig(),
        updater=fake_updater if fake_updater is not None else FakeUpdater(),
        status_path=status_path, install_dir="/opt/wb-energy-meter",
        http_port=8080,
    )
    return create_app(state), state


def test_api_check_disabled_returns_503():
    app, _ = make_client(update_config=FakeUpdateConfig(enabled=False))
    r = app.test_client().get("/api/update/check")
    assert r.status_code == 503
    print("[OK] GET /api/update/check при enabled=false -> 503")


def test_api_check_ok():
    app, _ = make_client()
    r = app.test_client().get("/api/update/check")
    assert r.status_code == 200
    body = r.get_json()
    assert body["update_available"] is True
    assert body["remote"]["commit"] == "def5678"
    assert body["current"]["commit"] == "abc1234"
    assert body["allow_from_ui"] is True
    print("[OK] GET /api/update/check -> 200, update_available корректен")


def test_api_check_network_error_502():
    fake = FakeUpdater(check_error=UpdateCheckError("не удалось подключиться"))
    app, _ = make_client(fake_updater=fake)
    r = app.test_client().get("/api/update/check")
    assert r.status_code == 502
    assert "error" in r.get_json()
    print("[OK] GET /api/update/check при сетевой ошибке -> 502")


def test_api_start_forbidden_when_allow_from_ui_false():
    app, _ = make_client(
        update_config=FakeUpdateConfig(allow_from_ui=False))
    r = app.test_client().post("/api/update/start",
                                json={"commit": "def5678"})
    assert r.status_code == 403
    print("[OK] POST /api/update/start при allow_from_ui=false -> 403")


def test_api_start_disabled_returns_503():
    app, _ = make_client(update_config=FakeUpdateConfig(enabled=False))
    r = app.test_client().post("/api/update/start",
                                json={"commit": "def5678"})
    assert r.status_code == 503
    print("[OK] POST /api/update/start при enabled=false -> 503")


def test_api_start_already_in_progress_409():
    fake = FakeUpdater(status={"state": "installing"})
    app, _ = make_client(fake_updater=fake)
    r = app.test_client().post("/api/update/start",
                                json={"commit": "def5678"})
    assert r.status_code == 409
    assert fake.start_update_calls == [], \
        "start_update не должен вызываться, если обновление уже идёт"
    print("[OK] POST /api/update/start при статусе installing -> 409, "
          "start_update не вызван")


def test_api_start_stale_commit_409():
    fake = FakeUpdater()  # remote.commit == "def5678"
    app, _ = make_client(fake_updater=fake)
    r = app.test_client().post("/api/update/start",
                                json={"commit": "устарел-abc"})
    assert r.status_code == 409
    assert fake.start_update_calls == [], \
        "start_update не должен вызываться при несовпадении commit"
    print("[OK] POST /api/update/start с чужим commit -> 409, "
          "start_update не вызван")


def test_api_start_missing_commit_400():
    app, _ = make_client()
    r = app.test_client().post("/api/update/start", json={})
    assert r.status_code == 400
    print("[OK] POST /api/update/start без commit -> 400")


def test_api_start_success_202():
    fake = FakeUpdater()
    app, _ = make_client(fake_updater=fake)
    r = app.test_client().post("/api/update/start",
                                json={"commit": "def5678"})
    assert r.status_code == 202
    assert len(fake.start_update_calls) == 1
    assert fake.start_update_calls[0]["expected_sha"] == "def5678"
    print("[OK] POST /api/update/start с верным commit -> 202, "
          "start_update вызван с expected_sha из remote")


def test_api_status_without_file_returns_idle():
    with tempfile.TemporaryDirectory() as d:
        missing_path = os.path.join(d, "update-status.json")
        # Реальный updater (не подмена) — файла нет вовсе.
        from wb_energy_meter import updater as real_updater
        state = _AppState(
            registry=FakeRegistry(), meters_repo=None,
            is_mqtt_connected=lambda: True,
            mqtt_message_count=lambda: 0, mqtt_error_count=lambda: 0,
            wb_db_client=None, consumption_service=None, started_at=0,
            update_config=FakeUpdateConfig(), updater=real_updater,
            status_path=missing_path, install_dir="/opt/wb-energy-meter",
        )
        app = create_app(state)
        r = app.test_client().get("/api/update/status")
        assert r.status_code == 200
        assert r.get_json() == {"state": "idle"}
        print("[OK] GET /api/update/status без файла -> 200 {'state':'idle'}, никогда не 500")


# ---------------------------------------------------------------------
# 5. start_update не должен реально ничего запускать
# ---------------------------------------------------------------------

def test_start_update_uses_systemd_run_without_launching():
    with tempfile.TemporaryDirectory() as d:
        status_path = os.path.join(d, "update-status.json")
        captured = {}

        def fake_launcher(cmd, env=None):
            captured["cmd"] = cmd
            captured["env"] = env
            # Ничего не запускаем по-настоящему.

        result = start_update(
            install_dir="/opt/wb-energy-meter",
            status_path=status_path,
            repo_owner="9043366188-dot", repo_name="wb-energy-meter",
            ref="main", expected_sha="def5678", http_port=8080,
            process_launcher=fake_launcher,
        )
        assert "cmd" in captured, "процесс не был 'запущен' через подмену"
        cmd = captured["cmd"]
        assert "systemd-run" in cmd, f"нет systemd-run в команде: {cmd}"
        assert any("self-update.sh" in part for part in cmd), \
            f"нет пути к self-update.sh в команде: {cmd}"
        assert "/opt/wb-energy-meter/scripts/self-update.sh" in cmd
        assert result["state"] == "starting"
        # Статус действительно записан на диск (реальный write_status).
        on_disk = read_status(status_path)
        assert on_disk["state"] == "starting"
        assert on_disk["to_commit"] == "def5678"
        print("[OK] start_update строит команду с systemd-run и путём к "
              "self-update.sh, ничего реально не запуская")


def test_start_update_raises_when_already_active():
    with tempfile.TemporaryDirectory() as d:
        status_path = os.path.join(d, "update-status.json")
        write_status(status_path, state="installing")
        called = []

        def fake_launcher(cmd, env=None):
            called.append(cmd)

        try:
            start_update(
                install_dir="/opt/wb-energy-meter", status_path=status_path,
                repo_owner="o", repo_name="r", ref="main",
                expected_sha="deadbeef", http_port=8080,
                process_launcher=fake_launcher)
            assert False, "должен был бросить UpdateInProgressError"
        except UpdateInProgressError:
            pass
        assert called == [], "процесс не должен запускаться при активном обновлении"
        print("[OK] start_update бросает UpdateInProgressError, если "
              "обновление уже идёт, и ничего не запускает")


if __name__ == "__main__":
    test_is_update_available_same_sha()
    test_is_update_available_different_sha()
    test_is_update_available_no_installed_commit()

    test_parse_version_double_quotes()
    test_parse_version_single_quotes()
    test_parse_version_extra_spaces()
    test_parse_version_in_full_file_text()
    test_parse_version_missing()

    test_write_then_read_status_full()
    test_write_status_merges_fields()
    test_read_status_missing_file()
    test_read_status_broken_json()
    test_write_status_is_atomic_no_tmp_leftover()

    test_api_check_disabled_returns_503()
    test_api_check_ok()
    test_api_check_network_error_502()
    test_api_start_forbidden_when_allow_from_ui_false()
    test_api_start_disabled_returns_503()
    test_api_start_already_in_progress_409()
    test_api_start_stale_commit_409()
    test_api_start_missing_commit_400()
    test_api_start_success_202()
    test_api_status_without_file_returns_idle()

    test_start_update_uses_systemd_run_without_launching()
    test_start_update_raises_when_already_active()

    print("\nВсе тесты Шага 9 (самообновление) пройдены.")

"""Тесты партии 15, этап 15.2: старые записывающие маршруты `/api/registry/*` и пишущие команды
CLI закрыты после поколения домена 2 (wb_energy_meter/legacy_guard.py; решения R2 и «CLI»).

Самостоятельный скрипт (не pytest):
    python tests/test_step73_legacy_write_guard.py

Три случая из плана (§8, 15.2), для маршрутов и для CLI:
  * поколение 1 (свежая или ещё не перенесённая БД) — запись работает, как раньше;
  * поколение 2 — запись отвечает 409 `moved_to_v2` с русским сообщением и новым маршрутом,
    сущность в БД не меняется (CLI — код выхода 3 и подсказка);
  * чтение и диагностические команды работают в обоих случаях.
Плюс сценарий старого внешнего клиента (§9.3) и правило «новый маршрут под /api/registry/
закрыт по умолчанию».
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import domain_generation as dg
from wb_energy_meter import legacy_guard
from wb_energy_meter.api import _AppState, create_app
from wb_energy_meter.db import Database
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.repo import GroupRepo, KvRepo, MeterRepo

PY = sys.executable


def make_env():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    groups = GroupRepo(db)
    meters = MeterRepo(db, groups)
    state = _AppState(
        registry=MeterRegistry(), meters_repo=meters, groups_repo=groups,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None,
        consumption_service=None, started_at=time.time(), db=db)
    return create_app(state).test_client(), db, path


def to_generation_2(db):
    """Как первая запись в v2-домен: ключи поколения в той же транзакции (domain_generation)."""
    with db.transaction() as c:
        dg.mark_v2_domain_write(c)
    assert dg.get_domain_generation(KvRepo(db)) == 2


def jpost(client, url, body, method="post"):
    return getattr(client, method)(url, data=json.dumps(body),
                                   content_type="application/json")


def registry_state(client):
    """Снимок старого реестра для сравнения «до / после»."""
    return (client.get("/api/registry/meters").get_json(),
            client.get("/api/registry/groups").get_json())


def group_id(client, name):
    return next(g["id"] for g in client.get("/api/registry/groups").get_json()["groups"] if g["name"] == name)


def seed(client):
    """Счётчик dev-1 в группе «Цех» и пустая группа «Склад»; возвращает id группы «Склад»."""
    r = jpost(client, "/api/registry/meters", {"device_id": "dev-1", "display_name": "Станок", "group": "Цех"})
    assert r.status_code == 201, r.get_json()
    r = jpost(client, "/api/registry/groups", {"name": "Склад"})
    assert r.status_code in (200, 201), r.get_json()
    return group_id(client, "Склад")


# 7 записывающих маршрутов старого реестра: (метод, путь, тело)
def write_calls(group_id):
    return [
        ("post", "/api/registry/meters", {"device_id": "dev-2", "display_name": "Новый"}),
        ("patch", "/api/registry/meters/dev-1", {"display_name": "Переименован"}),
        ("patch", "/api/registry/meters/dev-1/role", {"role": "input"}),
        ("delete", "/api/registry/meters/dev-1", None),
        ("post", "/api/registry/groups", {"name": "Ещё одна"}),
        ("patch", "/api/registry/groups/%d" % group_id, {"name": "Склад-2"}),
        ("delete", "/api/registry/groups/%d" % group_id, None),
    ]


def call(client, method, url, body):
    if body is None:
        return getattr(client, method)(url)
    return jpost(client, url, body, method)


# ----------------------------------------------------------------- маршруты

def test_generation_1_writes_work():
    client, db, path = make_env()
    try:
        assert dg.get_domain_generation(KvRepo(db)) == 1
        gid = seed(client)
        for method, url, body in write_calls(gid):
            r = call(client, method, url, body)
            assert r.status_code < 300, (method, url, r.status_code, r.get_json())
        print("[OK] поколение 1: все 7 записывающих маршрутов работают, как раньше")
    finally:
        db.close(); os.unlink(path)


def test_generation_2_writes_are_409_and_change_nothing():
    client, db, path = make_env()
    try:
        gid = seed(client)
        to_generation_2(db)
        before = registry_state(client)
        for method, url, body in write_calls(gid):
            r = call(client, method, url, body)
            data = r.get_json()
            assert r.status_code == 409, (method, url, r.status_code, data)
            assert data["code"] == "moved_to_v2", data
            assert data["error"] == data["message"] and "/api/v2" in data["message"], data
            assert url in data["message"] and data["replacement"].strip(), data
            # сообщение по-русски и с указанием, где это делается теперь
            assert any(ch in data["message"] for ch in "абвгдежзиклмнопрстуфхцчшщыэюя"), data
        assert registry_state(client) == before, "запись, закрытая стражем, изменила реестр"
        print("[OK] поколение 2: 7 маршрутов → 409 moved_to_v2 с новым маршрутом, реестр не изменился")
    finally:
        db.close(); os.unlink(path)


def test_generation_2_reads_still_work():
    client, db, path = make_env()
    try:
        seed(client)
        to_generation_2(db)
        for url in ("/api/registry/meters", "/api/registry/meters/dev-1", "/api/registry/groups",
                    "/api/meters", "/api/status", "/health"):
            r = client.get(url)
            assert r.status_code == 200, (url, r.status_code)
        print("[OK] поколение 2: чтение старого реестра работает (переходный цикл)")
    finally:
        db.close(); os.unlink(path)


def test_new_registry_write_route_is_closed_by_default():
    """Правило общее: любая запись под /api/registry/ после поколения 2 закрыта — и маршрут,
    которого сегодня нет; другие префиксы страж не трогает."""
    client, db, path = make_env()
    try:
        to_generation_2(db)
        r = jpost(client, "/api/registry/something-new", {"x": 1})
        assert r.status_code == 409 and r.get_json()["code"] == "moved_to_v2", r.get_json()
        assert not legacy_guard.is_legacy_write("GET", "/api/registry/meters")
        assert not legacy_guard.is_legacy_write("POST", "/api/v2/points")
        assert not legacy_guard.is_legacy_write("POST", "/api/registrybla")
        assert not legacy_guard.is_legacy_write("PATCH", "/api/plans/1")
        print("[OK] запись под /api/registry/ закрыта по умолчанию; /api/v2 и чужие префиксы — нет")
    finally:
        db.close(); os.unlink(path)


def test_old_external_client_scenario():
    """§9.3: старый внешний клиент (скрипт, не знающий про v2) после первой записи в v2 получает
    понятный машинный ответ, а его чтение по-прежнему работает. Тело без Content-Type, как у
    самых простых клиентов, — страж отвечает до разбора тела."""
    client, db, path = make_env()
    try:
        seed(client)
        to_generation_2(db)
        before = registry_state(client)
        r = client.post("/api/registry/meters", data=json.dumps({"device_id": "dev-9", "display_name": "Из скрипта"}))
        assert r.status_code == 409, r.status_code
        assert r.get_json()["code"] == "moved_to_v2"
        r = client.patch("/api/registry/meters/dev-1", data="не json вообще")
        assert r.status_code == 409 and r.get_json()["code"] == "moved_to_v2"
        assert registry_state(client) == before
        names = [m["display_name"] for m in client.get("/api/registry/meters").get_json()["items"]]
        assert names == ["Станок"], names
        print("[OK] сценарий старого внешнего клиента: 409 с кодом, чтение целое, запись не прошла")
    finally:
        db.close(); os.unlink(path)


# ------------------------------------------------- аудит экранов набора v2

JS_DIR = os.path.join(REPO_ROOT, "wb_energy_meter", "static", "js")
INDEX = os.path.join(REPO_ROOT, "wb_energy_meter", "static", "index.html")
# Экраны классики, которым разрешено ходить в старый реестр (их запись после поколения 2 закрыта стражем,
# а экран показывает сообщение сервера); у экранов v2 таких обращений быть не должно.
CLASSIC_JS = {"settings.js", "zones.js"}
_METHOD_RX = None


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def legacy_write_functions():
    """Имена методов app(), в теле которых есть запись в /api/registry/ (POST/PATCH/PUT/DELETE)."""
    import re
    names = {}
    head = re.compile(r"^      (?:async )?(\w+)\(", re.M)
    for fn in sorted(os.listdir(JS_DIR)):
        if not fn.endswith(".js"):
            continue
        text = _read(os.path.join(JS_DIR, fn))
        marks = [(m.start(), m.group(1)) for m in head.finditer(text)] + [(len(text), None)]
        for (a, name), (b, _n) in zip(marks, marks[1:]):
            body = text[a:b]
            for m in re.finditer(r"/api/registry/", body):
                window = body[m.start(): m.start() + 260]
                if re.search(r"method\s*:\s*['\"](POST|PATCH|PUT|DELETE)['\"]", window, re.I):
                    names.setdefault(name, set()).add(fn)
    return names


def classic_regions(html):
    """Отрезки index.html внутри <template x-if="uiSet=='classic'"> … </template> (с вложенностью)."""
    import re
    regions = []
    for m in re.finditer(r"<template[^>]*x-if=\"uiSet=='classic'\"[^>]*>", html):
        depth, pos = 1, m.end()
        for t in re.finditer(r"<template\b|</template>", html[pos:]):
            depth += 1 if t.group(0).startswith("<template") else -1
            if depth == 0:
                regions.append((m.start(), pos + t.end()))
                break
    return regions


def test_no_v2_screen_writes_to_legacy_registry():
    """15.2, аудит: записи в старый реестр делают только экраны классики — и только из блоков
    <template x-if="uiSet=='classic'"> разметки; ни один файл экрана v2 в старый реестр не пишет."""
    import re
    writers = legacy_write_functions()
    assert writers, "аудит ничего не нашёл — разбор исходников сломался"
    outside = {fn for files in writers.values() for fn in files} - CLASSIC_JS
    assert not outside, "запись в /api/registry/ из файлов не-классики: %s" % sorted(outside)
    html = _read(INDEX)
    regions = classic_regions(html)
    assert regions, "в index.html не найден блок uiSet=='classic'"
    # Окна подтверждения («Удалить зону?», «Удалить счётчик?») лежат в разметке вне блока классики, но
    # открываются только методами из блока классики: проверяем их по тому, кто их открывает.
    opened_by = {"confirmDeleteZone": "askDeleteZone", "confirmDelete": "askDelete"}
    bad = []
    for name in writers:
        probe = opened_by.get(name, name)
        hits = [m.start() for m in re.finditer(r"\b%s\(" % re.escape(probe), html)]
        if name in opened_by and not hits:
            bad.append("%s: не найден тот, кто его открывает (%s)" % (name, probe))
        for pos in hits:
            if not any(a <= pos < b for a, b in regions):
                bad.append("%s @%d" % (probe, pos))
    assert not bad, "запись в старый реестр вызывается вне блока классики: %s" % bad
    print("[OK] аудит: %d методов пишут в /api/registry/ (%s) — только файлы классики и только "
          "из блоков uiSet=='classic'; экраны v2 в старый реестр не пишут" % (len(writers), ", ".join(sorted(writers))))


# ---------------------------------------------------------------------- CLI

def cli(path, *args, input_text=None):
    p = subprocess.run([PY, "-m", "wb_energy_meter.cli", "--db-path", path] + list(args),
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, input=input_text)
    return p.returncode, p.stdout, p.stderr


def cli_writes(group_id):
    return [
        ("meter", "add", "dev-cli", "--name", "Из CLI"),
        ("meter", "rename", "dev-1", "--name", "Другое имя"),
        ("meter", "group", "dev-1", "--group", "Другая"),
        ("meter", "role", "dev-1", "--role", "input"),
        ("meter", "enable", "dev-1"),
        ("meter", "disable", "dev-1"),
        ("meter", "remove", "dev-1", "-y"),
        ("group", "remove", str(group_id), "-y"),
        ("scan", "--add-all"),
    ]


def test_cli_generation_1_writes_work():
    client, db, path = make_env()
    db.close()
    try:
        rc, out, err = cli(path, "meter", "add", "dev-g1", "--name", "Первое поколение")
        assert rc == 0, (rc, out, err)
        rc, out, err = cli(path, "meter", "rename", "dev-g1", "--name", "Переименован")
        assert rc == 0, (rc, out, err)
        rc, out, err = cli(path, "meter", "remove", "dev-g1", "-y")
        assert rc == 0, (rc, out, err)
        print("[OK] CLI, поколение 1: meter add/rename/remove работают, как раньше")
    finally:
        os.unlink(path)


def test_cli_generation_2_writes_exit_3_and_change_nothing():
    client, db, path = make_env()
    gid = seed(client)
    to_generation_2(db)
    db.close()
    try:
        for argv in cli_writes(gid):
            rc, out, err = cli(path, *argv)
            assert rc == legacy_guard.EXIT_MOVED_TO_V2 == 3, (argv, rc, out, err)
            assert "новой модели учёта" in err and "v2" in err, (argv, err)
            assert out.strip() == "", (argv, out)
        # диагностические команды работают и в поколении 2
        for argv in (("meter", "list"), ("meter", "show", "dev-1"), ("group", "list"),
                     ("db", "status"), ("aggregates", "status")):
            rc, out, err = cli(path, *argv)
            assert rc == 0, (argv, rc, out, err)
        # и ничего не изменилось
        db2 = Database(path=path); db2.open()
        try:
            g2 = GroupRepo(db2); m2 = MeterRepo(db2, g2)
            assert [m.device_id for m in m2.list_all()] == ["dev-1"]
            assert m2.get_by_device_id("dev-1").display_name == "Станок"
            assert sorted(g.name for g in g2.list_all()) == ["Склад", "Цех"]
        finally:
            db2.close()
        print("[OK] CLI, поколение 2: 9 пишущих команд → код 3 и подсказка; диагностические работают; БД не тронута")
    finally:
        os.unlink(path)


def test_cli_write_classification():
    class A:  # минимальная замена argparse.Namespace
        def __init__(self, **kw): self.__dict__.update(kw)
    assert legacy_guard.cli_write_key(A(cmd="meter", subcmd="add")) == ("meter", "add")
    assert legacy_guard.cli_write_key(A(cmd="meter", subcmd="list")) is None
    assert legacy_guard.cli_write_key(A(cmd="meter", subcmd="show")) is None
    assert legacy_guard.cli_write_key(A(cmd="group", subcmd="remove")) == ("group", "remove")
    assert legacy_guard.cli_write_key(A(cmd="group", subcmd="list")) is None
    assert legacy_guard.cli_write_key(A(cmd="scan", add_all=True)) == ("scan", "--add-all")
    assert legacy_guard.cli_write_key(A(cmd="scan", add_all=False)) is None
    assert legacy_guard.cli_write_key(A(cmd="db", subcmd="vacuum")) is None
    assert legacy_guard.cli_write_key(A(cmd="aggregates", subcmd="recompute")) is None
    print("[OK] классификация команд CLI: пишущие и диагностические")


if __name__ == "__main__":
    test_generation_1_writes_work()
    test_generation_2_writes_are_409_and_change_nothing()
    test_generation_2_reads_still_work()
    test_new_registry_write_route_is_closed_by_default()
    test_old_external_client_scenario()
    test_no_v2_screen_writes_to_legacy_registry()
    test_cli_write_classification()
    test_cli_generation_1_writes_work()
    test_cli_generation_2_writes_exit_3_and_change_nothing()
    print("[ALL OK] test_step73_legacy_write_guard")

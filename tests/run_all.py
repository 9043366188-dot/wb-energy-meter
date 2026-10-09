#!/usr/bin/env python3
"""Единый раннер тестов (партия 10, этап D, F2,
docs/TZ-batch10-reliability-and-load.md §4).

До этой партии ci.yml перечислял каждый тестовый файл отдельной строкой
руками — 87 строк на 45 файлов в двух job'ах. Это уже реально стреляло:
test_step40_topology_balance.py и test_step41_planv3_api.py (партия 7)
не были зарегистрированы вообще ни в одном job'е до партии 8, где это
заметили случайно. Раннер ходит по диску сам (`glob.glob`), а не по
руками составленному списку — новый tests/test_stepNN_*.py файл
подхватывается автоматически, без правки ci.yml.

НЕ трогает tests/browser/ — у него свой раннер, tests/browser/run.py
(другой характер тестов: поднимают реальный Chromium через Playwright).

Каждый tests/test_step*.py запускается ОТДЕЛЬНЫМ процессом (как и
раньше в ci.yml — `python tests/test_stepNN_....py`), а не импортом в
том же интерпретаторе: файлы этой кодовой базы не написаны с расчётом
на совместное исполнение в одном процессе (общие имена модулей,
побочные эффекты на уровне импорта) — так же обособленно их запускал
и человек руками, и старый ci.yml.

Три файла (test_step3_daemon.py, test_step3_e2e.py, test_step4_daemon.py)
реально устанавливают MQTT-соединение с mosquitto на 127.0.0.1:1883 и
падают без брокера — не потому что с кодом что-то не так, а потому что
песочница агента (или любая машина без локального mosquitto) не может
физически их выполнить. Раннер узнаёт такие файлы по имени — маска
"*_daemon.py" или ТОЧНО "*_e2e.py" (без хвостового "*" после e2e:
test_step34_e2e_ui_creation_path.py под "*_e2e.py" не подходит и не
попадает в список — он e2e по названию, но mosquitto ему не нужен,
проверено отдельно). Наивная проверка по содержимому файла (искать
"mosquitto"/"1883" текстом) была опробована и отброшена: она ложно
цепляет test_step10_wbserial.py (там просто пример конфига с
"port: 1883") и test_step28_reports_query.py (там просто комментарий
ПРО mosquitto, а не использование его), а на самом этом файле —
test_step48_runner.py — цепляет саму себя, потому что этот докстринг
упоминает оба слова. Маска по имени такой ловушки не создаёт. И:
  - при --skip-mosquitto пропускает их безусловно;
  - иначе сам пробует достучаться до 127.0.0.1:1883 — если брокер
    отвечает (как в job `test` ci.yml, где mosquitto поднимается ДО
    вызова этого раннера), гоняет их как обычно; если нет — пропускает
    их с явной пометкой SKIPPED, а не тихо и не как провал. Адрес
    проверки переопределяется WBEM_TEST_MQTT_PROBE=host:port — это
    нужно тестам самого раннера (test_step48), чтобы не зависеть от
    того, поднят ли mosquitto на машине.
Пропущенные так тесты не влияют на код выхода — раннер остаётся
зелёным и полезным без брокера, но НЕ прячет тот факт, что часть
покрытия не выполнилась: это видно в сводке.

Обязательная защита от "нечаянно отфильтровали" (та самая история с
test_step40/41): раннер сверяет число НАЙДЕННЫХ файлов с независимым
повторным glob() прямо перед печатью сводки и падает, если они
разошлись — то есть если что-то в отборе файлов ушло не в основной
список поиска, а не в осознанное сужение через --only.

Использование:
    python tests/run_all.py
    python tests/run_all.py --only "test_step4*"
    python tests/run_all.py --skip-mosquitto

Код выхода: 0, если все запущенные тесты прошли (пропущенные из-за
отсутствия mosquitto не считаются); ненулевой — если хоть один упал,
если раннер не нашёл ни одного файла, подходящего под --only, или если
сработала защита "нечаянно отфильтровали".
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import os
import socket
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
GLOB_PATTERN = os.path.join(TESTS_DIR, "test_step*.py")


def _discover():
    """Единственное место, откуда берётся список тестов -- голый glob
    по диску, без ручных списков имён."""
    return sorted(glob.glob(GLOB_PATTERN))


def _needs_mosquitto(path):
    """По имени файла, не по содержимому -- см. докстринг модуля:
    наивный поиск "mosquitto"/"1883" текстом внутри файла ложно цепляет
    файлы, которые просто УПОМИНАЮТ эти слова (пример конфига,
    комментарий, этот же докстринг), не требуя реального брокера."""
    base = os.path.basename(path)
    return fnmatch.fnmatch(base, "*_daemon.py") or fnmatch.fnmatch(base, "*_e2e.py")


PROBE_ENV = "WBEM_TEST_MQTT_PROBE"
DEFAULT_PROBE = ("127.0.0.1", 1883)


def _probe_address():
    """Куда стучаться, чтобы понять, поднят ли брокер.

    По умолчанию 127.0.0.1:1883 -- так работает job `test` в ci.yml и
    человек у себя. Переменная окружения WBEM_TEST_MQTT_PROBE=host:port
    нужна тестам самого раннера (test_step48): им надо самим решать,
    «есть брокер» или «нет», независимо от того, поднят ли mosquitto на
    машине. Без этого тест, молча предполагавший отсутствие брокера,
    валил весь job `test`, где mosquitto поднимается ДО раннера.

    Кривое значение -- ValueError: молча вернуться к умолчанию значило бы
    проверять не тот адрес, который просили."""
    raw = os.environ.get(PROBE_ENV, "").strip()
    if not raw:
        return DEFAULT_PROBE
    host, sep, port = raw.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(
            f"{PROBE_ENV}={raw!r}: ожидается host:port, например 127.0.0.1:1883")
    return host, int(port)


def _mosquitto_reachable(host="127.0.0.1", port=1883, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run(argv=None):
    parser = argparse.ArgumentParser(
        description="Единый раннер tests/test_step*.py (партия 10, F2)")
    parser.add_argument(
        "--only", metavar="PATTERN", default=None,
        help="fnmatch-шаблон по имени файла (например test_step4*), "
             "сужает выборку; пустое совпадение -- ошибка (вероятная опечатка)")
    parser.add_argument(
        "--skip-mosquitto", action="store_true",
        help="безусловно пропустить тесты, которым нужен mosquitto "
             "(иначе раннер сам проверит 127.0.0.1:1883 или адрес из "
             f"{PROBE_ENV}=host:port)")
    args = parser.parse_args(argv)

    try:
        probe_host, probe_port = _probe_address()
    except ValueError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2
    probe_label = f"{probe_host}:{probe_port}"

    all_files = _discover()
    on_disk_count = len(glob.glob(GLOB_PATTERN))
    if len(all_files) < on_disk_count:
        print(
            f"ОШИБКА РАННЕРА: найдено {len(all_files)} файлов, а на диске их "
            f"{on_disk_count} -- часть tests/test_step*.py потерялась при "
            f"отборе ДО --only. Это баг раннера, а не осознанное сужение "
            f"(см. историю test_step40/41 из партии 7/8 в docs).",
            file=sys.stderr)
        return 2

    print(f"Найдено файлов tests/test_step*.py: {len(all_files)}")

    if args.only:
        selected = [f for f in all_files
                    if fnmatch.fnmatch(os.path.basename(f), args.only)]
        if not selected:
            print(f"ОШИБКА: --only {args.only!r} не совпал ни с одним файлом "
                  f"из {len(all_files)} найденных -- проверьте шаблон "
                  f"(похоже на опечатку).", file=sys.stderr)
            return 2
        print(f"--only {args.only!r}: выбрано {len(selected)} из {len(all_files)}")
    else:
        selected = all_files

    broker_up = None  # вычисляем лениво, только если реально понадобится
    results = []  # (path, "PASS"|"FAIL"|"SKIP", details)
    for path in selected:
        base = os.path.basename(path)
        if _needs_mosquitto(path):
            if args.skip_mosquitto:
                results.append((base, "SKIP", "--skip-mosquitto"))
                print(f"SKIP {base} (--skip-mosquitto)")
                continue
            if broker_up is None:
                broker_up = _mosquitto_reachable(probe_host, probe_port)
            if not broker_up:
                results.append((base, "SKIP", f"нет mosquitto на {probe_label}"))
                print(f"SKIP {base} (нет mosquitto на {probe_label})")
                continue

        start = time.time()
        proc = subprocess.run(
            [sys.executable, path], cwd=REPO_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        elapsed = time.time() - start
        if proc.returncode == 0:
            results.append((base, "PASS", f"{elapsed:.1f}s"))
            print(f"PASS {base} ({elapsed:.1f}s)")
        else:
            results.append((base, "FAIL", f"код {proc.returncode}"))
            print(f"FAIL {base} (код {proc.returncode}, {elapsed:.1f}s)")
            # Вывод упавшего теста -- сразу, а не только в сводке: если
            # раннер прервётся или сводка потеряется в логе CI, причина
            # уже была напечатана.
            print(f"--- вывод {base} ---")
            print(proc.stdout)
            print(f"--- конец вывода {base} ---")

    passed = [r for r in results if r[1] == "PASS"]
    failed = [r for r in results if r[1] == "FAIL"]
    skipped = [r for r in results if r[1] == "SKIP"]

    print("\n--- Сводка ---")
    print(f"Найдено: {len(all_files)}; выбрано: {len(selected)}; "
          f"пройдено: {len(passed)}; упало: {len(failed)}; "
          f"пропущено: {len(skipped)}")
    for base, status, details in results:
        if status != "PASS":
            print(f"  {status:5} {base} -- {details}")

    if failed:
        print(f"\nПРОВАЛ: {len(failed)} из {len(selected)}")
        return 1

    print(f"\nВСЁ ЗЕЛЁНОЕ: {len(passed)} пройдено"
          + (f", {len(skipped)} пропущено" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(run())

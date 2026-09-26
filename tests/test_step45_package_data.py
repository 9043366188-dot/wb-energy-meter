"""Шаг 45: всё содержимое wb_energy_meter/static/ обязано попадать в
собираемый пакет.

ПРОБЛЕМА, ради которой тест написан (найдено 27.09.2026, после партии 9).
Глоб `static/*` в `[tool.setuptools.package-data]` НЕ рекурсивный:
вложенные каталоги он не захватывает. Партия 9 вынесла весь JS
интерфейса в `static/js/*.js`, а строку в `pyproject.toml` добавить
забыли — в собранном колесе не оказалось ни одного из 11 файлов. На
контроллере это выглядело бы так: `index.html` ставится, ссылается на
11 скриптов, каждый отдаёт 404, интерфейс мёртв. Третий белый экран в
истории проекта.

Почему это не поймал ни один существующий тест: все они читают файлы из
рабочего дерева, где `static/js/` на месте. Job `lint` в CI пакет
собирает (`python -m build`), но внутрь собранного пакета не смотрит.

Тест намеренно СТАТИЧЕСКИЙ — разбирает `pyproject.toml` и сравнивает со
списком файлов на диске, не собирая пакет: сборка тянет сеть и занимает
десятки секунд, а правило проверяется и так. Сборку с проверкой
содержимого добавляет job `lint` отдельным шагом.

Самостоятельный скрипт (не pytest):
    python tests/test_step45_package_data.py
"""

from __future__ import annotations

import fnmatch
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

PKG_DIR = os.path.join(REPO_ROOT, "wb_energy_meter")
PYPROJECT = os.path.join(REPO_ROOT, "pyproject.toml")


def _load_package_data_globs():
    """Достаёт список globs для пакета wb_energy_meter из pyproject.toml.

    Своими руками, а не через tomllib: на контроллере Python 3.9, где
    tomllib отсутствует, а тянуть зависимость ради одного списка нельзя
    (AGENTS.md — только stdlib + flask/paho-mqtt/pyyaml)."""
    with open(PYPROJECT, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    globs = []
    in_section = False
    in_pkg_list = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("["):
            in_section = line == "[tool.setuptools.package-data]"
            in_pkg_list = False
            continue
        if not in_section or line.startswith("#"):
            continue
        if line.startswith('"wb_energy_meter"') and "[" in line:
            in_pkg_list = True
            continue
        if in_pkg_list:
            if line.startswith("]"):
                in_pkg_list = False
                continue
            value = line.strip().rstrip(",").strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                globs.append(value[1:-1])
    return globs


def _matches(rel_path, pattern):
    """Совпадение пути с глобом ПО СЕГМЕНТАМ, как это делает setuptools.

    Через голый fnmatch делать нельзя, и это не теория: первая версия
    этого теста так и была написана и оказалась бесполезной — она
    оставалась зелёной даже после удаления строки "static/js/*" из
    pyproject.toml, ради которой писалась. Причина: fnmatch не знает
    про пути, и его `*` спокойно проходит через слэш, поэтому
    fnmatch("static/js/core.js", "static/*") == True. У setuptools `*`
    живёт внутри одного сегмента, и вложенный каталог без своей строки
    в пакет не попадает.

    Поэтому: делим на сегменты, требуем одинаковую длину и сверяем
    каждый сегмент отдельно."""
    rel_parts = rel_path.split("/")
    pat_parts = pattern.split("/")
    if len(rel_parts) != len(pat_parts):
        return False
    return all(fnmatch.fnmatchcase(r, p)
               for r, p in zip(rel_parts, pat_parts))


def test_every_static_file_is_covered_by_package_data():
    globs = _load_package_data_globs()
    assert globs, "не удалось прочитать package-data для wb_energy_meter"

    static_root = os.path.join(PKG_DIR, "static")
    uncovered = []
    checked = 0
    for dirpath, dirnames, filenames in os.walk(static_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, PKG_DIR).replace(os.sep, "/")
            checked += 1
            if not any(_matches(rel, g) for g in globs):
                uncovered.append(rel)

    assert not uncovered, (
        "Эти файлы НЕ попадут в собранный пакет (глоб static/* не "
        "рекурсивный — нужна отдельная строка на каждый подкаталог в "
        "[tool.setuptools.package-data] в pyproject.toml):\n  "
        + "\n  ".join(sorted(uncovered)))

    print("[OK] все %d файлов static/ покрыты package-data (%d globs)"
          % (checked, len(globs)))


def test_every_script_referenced_by_index_html_is_covered():
    """Парная, более узкая проверка: каждый файл, на который index.html
    ссылается через src/href="/static/…", обязан и существовать, и
    попадать в пакет. Первое уже проверяют другие тесты и
    /api/selfcheck, второе не проверял никто."""
    import re

    globs = _load_package_data_globs()
    with open(os.path.join(PKG_DIR, "static", "index.html"),
              "r", encoding="utf-8") as f:
        html = f.read()

    refs = sorted(set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html)))
    assert refs, "в index.html не нашлось ни одной ссылки на /static/"

    problems = []
    for ref in refs:
        rel = "static/" + ref[len("/static/"):]
        if not os.path.isfile(os.path.join(PKG_DIR, rel)):
            problems.append(rel + " — файла нет на диске")
        elif not any(_matches(rel, g) for g in globs):
            problems.append(rel + " — не попадёт в пакет")

    assert not problems, (
        "Ссылки из index.html, которые сломаются после установки:\n  "
        + "\n  ".join(problems))

    print("[OK] все %d ссылок /static/ из index.html существуют и "
          "попадают в пакет" % len(refs))


if __name__ == "__main__":
    test_every_static_file_is_covered_by_package_data()
    test_every_script_referenced_by_index_html_is_covered()
    print("\nВсе тесты Шага 45 (упаковка статики) пройдены.")

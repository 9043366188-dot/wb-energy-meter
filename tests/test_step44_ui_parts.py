"""Тесты Шага 44 (партия 9, F5): целостность разбиения JS на
wb_energy_meter/static/js/*.js (docs/TZ-batch9-split-frontend.md §4).

До этой партии весь JS интерфейса жил одним куском в index.html.
Партия 9 разнесла его на 11 файлов (core + 10 экранов), собираемых в
рантайме через window.WBEM.parts. Этот файл проверяет саму механику
разбиения — а не поведение конкретных экранов (это по-прежнему
tests/browser/test_b04_app_surface.py и tests/test_step6_webui.py):
- каждый static/js/*.js подключён в index.html ровно один раз;
- каждая ссылка <script src="/static/js/..."> существует на диске и
  не пуста;
- /api/selfcheck (build_selfcheck_result) видит эти файлы и honestly
  скажет ok=false, если один из них пропадёт;
- порядок <script>: вендорные библиотеки -> части интерфейса ->
  alpine.min.js (именно в этом порядке Alpine должен увидеть уже
  заполненный window.WBEM.parts, см. §3.2 ТЗ);
- ни у одного src нет "?v=" или любого другого query string — резолвер
  selfcheck не отрезает query string и решит, что файла нет (§2 ТЗ).

Самостоятельный скрипт (не pytest):
    python tests/test_step44_ui_parts.py
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.api import build_selfcheck_result

_STATIC_DIR = os.path.join(REPO_ROOT, "wb_energy_meter", "static")
_VENDOR_DIR = os.path.join(_STATIC_DIR, "vendor")
_JS_DIR = os.path.join(_STATIC_DIR, "js")


def _read_index():
    with open(os.path.join(_STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


def _js_files():
    return sorted(f for f in os.listdir(_JS_DIR) if f.endswith(".js"))


def test_each_js_file_included_exactly_once():
    html = _read_index()
    files = _js_files()
    assert files, "в static/js/ не найдено ни одного .js файла"
    for name in files:
        tag = '<script src="/static/js/%s"></script>' % name
        count = html.count(tag)
        assert count == 1, (
            "static/js/%s должен быть подключён РОВНО ОДИН раз тегом %r, "
            "найдено вхождений: %d" % (name, tag, count))
    print("[OK] каждый static/js/*.js подключён в index.html ровно один раз "
          "(%d файлов)" % len(files))


def test_every_referenced_script_exists_and_nonempty():
    html = _read_index()
    refs = re.findall(r'<script src="(/static/js/[^"]+)"></script>', html)
    assert refs, 'не нашлось ни одного <script src="/static/js/...">'
    for ref in refs:
        name = ref[len("/static/js/"):]
        path = os.path.join(_JS_DIR, name)
        assert os.path.isfile(path), "%s: файл не найден на диске" % ref
        assert os.path.getsize(path) > 0, "%s: файл пустой" % ref
    print('[OK] все %d ссылок <script src="/static/js/..."> существуют на '
          "диске и не пусты" % len(refs))


def test_selfcheck_ok_on_real_dir():
    result = build_selfcheck_result(_STATIC_DIR, _VENDOR_DIR, "test")
    assert result["ok"] is True, result["failed"]
    assert result["checked"] >= len(_js_files())
    print("[OK] /api/selfcheck: ok=true на реальных файлах репозитория "
          "(checked=%d, включая %d static/js/*.js)"
          % (result["checked"], len(_js_files())))


def test_selfcheck_fails_with_missing_part():
    """Убираем один вынесенный экран из копии дерева — selfcheck обязан
    честно сказать ok=false, а не притвориться, что всё в порядке
    (ровно то, из-за чего появилась самопроверка в Шаге 12: белый экран
    без единой ошибки в логе)."""
    tmp = tempfile.mkdtemp()
    try:
        static_copy = os.path.join(tmp, "static")
        shutil.copytree(_STATIC_DIR, static_copy)
        js_files = sorted(
            f for f in os.listdir(os.path.join(static_copy, "js"))
            if f.endswith(".js"))
        assert js_files
        victim = os.path.join(static_copy, "js", js_files[0])
        os.remove(victim)
        result = build_selfcheck_result(
            static_copy, os.path.join(static_copy, "vendor"), "test")
        assert result["ok"] is False, (
            "selfcheck должен был заметить отсутствующий %s" % js_files[0])
        reasons = [f["reason"] for f in result["failed"]]
        assert any("не найден" in r for r in reasons), result["failed"]
        print("[OK] /api/selfcheck: ok=false, если убрать один "
              "static/js/*.js (%s)" % js_files[0])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_script_order_vendor_then_parts_then_alpine():
    """§3.2 ТЗ: vendor -> все части (без defer/type=module) -> alpine.min.js
    с defer. Порядок важен: части кладут свои фабрики в
    window.WBEM.parts ДО того, как Alpine (загруженный с defer, то есть
    гарантированно после всех обычных <script>) вызовет app()."""
    html = _read_index()
    tags = re.findall(r'<script[^>]*\bsrc="(/static/[^"]+)"[^>]*></script>',
                       html)
    assert tags, "не нашлось ни одного <script src=...>"

    def kind(ref):
        if ref.startswith("/static/vendor/"):
            return "alpine" if "alpine" in ref else "vendor"
        if ref.startswith("/static/js/"):
            return "part"
        return "other"

    kinds = [kind(t) for t in tags]
    assert "other" not in kinds, (
        "неизвестный тип <script src=...>: %r" %
        [t for t, k in zip(tags, kinds) if k == "other"])
    assert kinds.count("alpine") == 1, "должен быть ровно один alpine.min.js"
    alpine_idx = kinds.index("alpine")
    assert alpine_idx == len(kinds) - 1, "alpine.min.js должен идти последним"

    before_alpine = kinds[:alpine_idx]
    assert "part" in before_alpine, "не нашлось ни одной части static/js/*.js"
    first_part = before_alpine.index("part")
    assert all(k == "vendor" for k in before_alpine[:first_part]), (
        "перед первым static/js/*.js должны идти только vendor-скрипты")
    assert all(k == "part" for k in before_alpine[first_part:]), (
        "после первого static/js/*.js не должно быть vendor-скриптов — "
        "порядок обязан быть vendor -> parts -> alpine, без чередования")

    alpine_tag_full = re.search(
        r'<script[^>]*\bsrc="' + re.escape(tags[alpine_idx]) +
        r'"[^>]*></script>', html).group(0)
    assert "defer" in alpine_tag_full, "alpine.min.js обязан грузиться с defer"
    for ref in tags:
        if kind(ref) == "part":
            part_tag_full = re.search(
                r'<script[^>]*\bsrc="' + re.escape(ref) + r'"[^>]*></script>',
                html).group(0)
            assert "defer" not in part_tag_full and "type=" not in part_tag_full, (
                "%s не должен грузиться с defer/type=module (§3.2, §5 ТЗ)"
                % ref)
    print("[OK] порядок <script>: vendor -> static/js/*.js (%d шт.) -> "
          "alpine.min.js (defer)" % kinds.count("part"))


def test_no_cache_busting_query_string():
    """?v=... в src ломает резолвер /api/selfcheck (он не отрезает query
    string) — см. docs/TZ-batch9-split-frontend.md §2."""
    html = _read_index()
    offenders = [m.group(1) for m in re.finditer(r'src="(/static/[^"]+)"', html)
                 if "?" in m.group(1)]
    assert not offenders, (
        "src=... содержит query string — selfcheck не отрежет её и решит, "
        "что файла нет: %r" % offenders)
    print('[OK] ни у одного src="/static/..." нет "?" (в т.ч. "?v=")')


if __name__ == "__main__":
    test_each_js_file_included_exactly_once()
    test_every_referenced_script_exists_and_nonempty()
    test_selfcheck_ok_on_real_dir()
    test_selfcheck_fails_with_missing_part()
    test_script_order_vendor_then_parts_then_alpine()
    test_no_cache_busting_query_string()
    print("\nВсе тесты Шага 44 (партия 9, F5: целостность static/js/*.js) "
          "пройдены.")

"""Тесты Шага 61 (партия 13, находки пилота П1–П6): статические проверки
интерфейса схемы — то, что не требует браузера и ловится дёшево.

Браузерные сценарии находок — tests/browser/test_b06 … test_b09. Здесь —
договорённости, которые легко разрушить правкой одного места из двух:

- карточка элемента схемы живёт в index.html ДВАЖДЫ (x-teleport принимает
  только литеральный селектор: «План v3» и «Структура» — разные вкладки),
  и обе копии обязаны быть дословно одинаковыми (П5/П6 «одна и та же карточка»);
- один словарь типов элементов (П2): подписи «Ввод / Щит / Ответвление /
  Нагрузка» — только в core.js; старых названий в интерфейсе нет;
- перенос легаси-данных (П3) остаётся ТОЛЬКО в классических «Настройках»;
  вкладки «План v2» в наборе v2 нет;
- «мест» в «Структуре» v2 нет (П6): ни кода дерева мест, ни форм;
- scheme.js подключён ровно один раз (общий контроль — test_step44);
- слова для оператора в docs/pilot-checklist.md те же, что в интерфейсе (П2).

Самостоятельный скрипт (не pytest):
    python tests/test_step61_scheme_ui.py
"""

from __future__ import annotations

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

_STATIC = os.path.join(REPO_ROOT, "wb_energy_meter", "static")
_JS = os.path.join(_STATIC, "js")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _index():
    return _read(os.path.join(_STATIC, "index.html"))


def _core():
    return _read(os.path.join(_JS, "core.js"))


def _strip_comments(text, is_js):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if is_js:
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        text = re.sub(r"(?m)(?<=[;,{}) ])\s//\s.*$", "", text)
    return text


def _all_ui_text():
    """Весь текст интерфейса (без комментариев): разметка и все части JS."""
    parts = [_strip_comments(_index(), False)]
    for name in sorted(os.listdir(_JS)):
        if name.endswith(".js"):
            parts.append(_strip_comments(_read(os.path.join(_JS, name)), True))
    return "\n".join(parts)


def _node_card_blocks():
    html = _index()
    begins = [m.start() for m in re.finditer(r"<!-- NODE-CARD:BEGIN", html)]
    ends = [m.start() for m in re.finditer(r"<!-- NODE-CARD:END -->", html)]
    assert len(begins) == 2 and len(ends) == 2, (
        "карточка элемента схемы должна быть в index.html ровно в двух местах "
        "(Структура и План v3); найдено BEGIN=%d END=%d" % (len(begins), len(ends)))
    blocks = []
    for b, e in zip(begins, ends):
        assert b < e, "NODE-CARD:END раньше NODE-CARD:BEGIN"
        blocks.append(html[b:e])
    return blocks


def test_node_card_copies_identical():
    a, b = _node_card_blocks()
    if a != b:
        la, lb = a.splitlines(), b.splitlines()
        for i, (x, y) in enumerate(zip(la, lb), 1):
            if x != y:
                raise AssertionError(
                    "копии карточки элемента расходятся на строке %d блока:\n  Структура: %r\n  План v3:   %r"
                    % (i, x.strip()[:140], y.strip()[:140]))
        raise AssertionError("копии карточки элемента разной длины: %d и %d строк" % (len(la), len(lb)))
    assert 'data-testid="node-card"' in a and 'data-testid="node-meter-form"' in a, \
        "в блоке нет самой карточки/формы «+ Счётчик»"
    print("[OK] две копии карточки элемента схемы идентичны (%d строк)" % len(a.splitlines()))


def test_scheme_js_included_once():
    html = _index()
    tag = '<script src="/static/js/scheme.js"></script>'
    assert html.count(tag) == 1, "scheme.js должен быть подключён ровно один раз"
    print("[OK] scheme.js подключён один раз")


def test_node_kind_dictionary_single_source():
    core = _core()
    kinds = re.findall(r"\{value:'(\w+)', label:'([^']+)',\s*hint:'([^']+)'\}", core)
    assert [(v, l) for v, l, _ in kinds] == [
        ("source", "Ввод"), ("panel", "Щит"), ("junction", "Ответвление"), ("load", "Нагрузка")], (
        "словарь NODE_KINDS в core.js изменён: %r" % (kinds,))
    for _, label, hint in kinds:
        assert "\n" not in hint and len(hint) <= 80, "подсказка типа %r — одна короткая строка" % label
    # все списки типов строятся из словаря: ни одного «своего» option с типом
    html = _index()
    for kind in ("source", "panel", "junction", "load"):
        bad = re.findall(r'<option[^>]*value="%s"[^>]*>' % kind, html)
        assert not bad, "в разметке свой <option> типа %r вместо словаря nodeKinds: %r" % (kind, bad)
    # старые названия типов вычищены из интерфейса
    text = _all_ui_text()
    old = ["Узел разветвления", "Промежуточный узел", "Узел сети", "+ Узел", "+ узел"]
    found = [w for w in old if w in text]
    assert not found, "в интерфейсе остались старые названия типов: %r" % found
    print("[OK] один словарь типов (Ввод/Щит/Ответвление/Нагрузка), старых названий нет")


def test_scheme_hint_and_meter_note_words():
    core = _core()
    assert "Схема — это вводы, щиты и нагрузки, соединённые линиями. " in core
    assert "Счётчик ставится на линию: тогда видно, сколько прошло по ней." in core
    assert "Счётчик ставится на линию, а не на сам элемент" in core
    html = _index()
    assert html.count('x-text="schemeEmptyHint"') >= 3, \
        "подсказка пустой схемы должна показываться на «Структуре», «Плане v3» и в «Плане v3» (панель)"
    assert "+ Элемент схемы" in html and "+ Узел" not in html
    print("[OK] подсказка пустой схемы и фраза «счётчик ставится на линию» — из словаря")


def test_legacy_migration_only_in_classic_settings():
    html = _index()
    core = _core()
    m = re.search(r"const UI_SET_TABS = \{(.*?)\};", core, re.S)
    assert m, "не найден UI_SET_TABS"
    v2 = re.search(r"v2:\s*\[([^\]]*)\]", m.group(1)).group(1)
    assert "planv2" not in v2, "вкладка «План v2» вернулась в набор v2: %s" % v2
    # единственное место, где запускают перенос — раздел классических «Настроек»
    assert html.count('migrateLegacyConfirm=true') == 1, \
        "кнопка переноса легаси-данных должна быть ровно одна (классические «Настройки»)"
    sec = html.index("<h2>Перенос легаси-данных</h2>")
    btn = html.index('migrateLegacyConfirm=true')
    assert sec < btn < sec + 3500, "кнопка переноса вынесена из раздела «Перенос легаси-данных»"
    assert 'data-testid="migrate-warning"' in html and "Для пилота не нужно" in html
    # пустой «Обзор» и вкладка «План v2» не предлагают перенос
    plain = _strip_comments(html, False)      # про перенос в комментариях говорить можно
    ov = plain.index('data-testid="overview-empty"')
    assert "легаси" not in plain[ov:ov + 1500].lower(), "пустой «Обзор» снова предлагает перенос легаси-данных"
    assert "Начните со схемы" in plain[ov:ov + 400]
    pv2 = plain.find("tab=='planv2'")
    if pv2 != -1:
        tail = plain[pv2:pv2 + 6000]
        assert "migrateLegacy" not in tail and "Перенести легаси" not in tail, \
            "в разделе «План v2» снова есть кнопка/текст переноса легаси-данных"
    print("[OK] перенос легаси-данных — только в классических «Настройках», с предупреждением")


def test_no_places_in_structure_v2():
    structure = _read(os.path.join(_JS, "structure.js"))
    scheme = _read(os.path.join(_JS, "scheme.js"))
    for dead in ("structLocationRows", "structSelectedLocationId", "locEditForm", "locForm",
                 "pointForm", "structSubmitLocation", "structSubmitPoint", "structSubmitNode",
                 "openLocationEdit", "saveLocationEdit", "archiveSelectedLocation",
                 "structMissingInfo", "structUnplacedPoints", "showPointOnPlan"):
        assert dead not in structure and dead not in scheme and dead not in _index(), \
            "в интерфейсе v2 остался код мест/старых форм: %s" % dead
    html = _index()
    s = html.index("<template x-if=\"tab=='structurev2'\">")
    e = html.index("x-show=\"tab=='planv3'\"", s)
    block = _strip_comments(html[s:e], False)
    for word in ("Без места", "Новое место", "нет места", "Дерево мест", "Места"):
        assert word not in block, "на «Структуре» осталось слово про места: %r" % word
    ov = _strip_comments(_read(os.path.join(_JS, "overview-v2.js")), True)
    assert "нет места" not in ov and "без места" not in ov.lower(), \
        "в «Чего не хватает»/«Обзоре» осталось «нет места»"
    print("[OK] «мест» в интерфейсе v2 нет: ни кода, ни форм, ни слов")


def test_confirm_texts():
    scheme = _read(os.path.join(_JS, "scheme.js"))
    assert ("История сохраняется; из текущего состава, итогов и балансов точка '+\n"
            "                 'уходит с этого момента.") in scheme, \
        "в окне архивации точки нет требуемой фразы про историю и «с этого момента»"
    for needed in ("Убрать с плана", "Архивировать узел", "Вывести из работы",
                   "Вывести линии из работы и архивировать", "Показать архивные",
                   "Убрать счётчик"):
        assert needed in scheme or needed in _index(), "нет подписи действия: %r" % needed
    print("[OK] подписи и тексты подтверждений на месте")


def test_operator_docs_use_interface_words():
    core = _core()
    checklist = _read(os.path.join(REPO_ROOT, "docs", "pilot-checklist.md"))
    for word in ("Ввод", "Щит", "Ответвление", "Нагрузка", "+ Счётчик",
                 "Счётчик ставится на линию"):
        assert word in checklist, "в docs/pilot-checklist.md нет слова интерфейса %r" % word
    hint = re.search(r"'(Схема — это вводы, щиты и нагрузки, соединённые линиями\. )'\s*\+\s*\n\s*'([^']+)'", core)
    assert hint, "не нашёл SCHEME_EMPTY_HINT в core.js"
    full = hint.group(1) + hint.group(2)
    assert full in " ".join(checklist.split()), \
        "подсказка пустой схемы в docs/pilot-checklist.md должна совпадать с интерфейсом слово в слово: %r" % full
    for old in ("Узел разветвления", "Промежуточный узел", "Узел сети", "Перенести легаси"):
        # в чек-листе допустимо упомянуть перенос как «не нужен», но не как шаг
        if old == "Перенести легаси":
            assert not re.search(r"(?m)^\s*\d+\.\s.*Перенести легаси", checklist), \
                "перенос легаси-данных снова назван шагом чек-листа"
        else:
            assert old not in checklist, "в чек-листе старое название типа: %r" % old
    print("[OK] docs/pilot-checklist.md говорит теми же словами, что интерфейс")


if __name__ == "__main__":
    test_node_card_copies_identical()
    test_scheme_js_included_once()
    test_node_kind_dictionary_single_source()
    test_scheme_hint_and_meter_note_words()
    test_legacy_migration_only_in_classic_settings()
    test_no_places_in_structure_v2()
    test_confirm_texts()
    test_operator_docs_use_interface_words()
    print("\nВсе тесты Шага 61 (партия 13: статика интерфейса схемы, П1–П6) пройдены.")

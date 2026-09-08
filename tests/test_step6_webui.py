"""Тесты Шага 6: отдача веб-интерфейса."""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter.api import create_app, _AppState, _load_static


# ---- Проверка баланса HTML-тегов (см. AGENTS.md: незакрытый <template>
# уже приводил к белому экрану без единой ошибки в консоли — самая
# дорогая ошибка в истории проекта). Снимаем <script>/<style>/комментарии,
# затем вручную сканируем строку посимвольно с учётом кавычек в атрибутах
# (naive-регексп по "<[^>]*>" ломается на Alpine-выражениях вида
# `:class="r.delta>0?'a':'b'"`, где внутри значения атрибута встречаются
# буквальные '<'/'>').

_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


def _strip_scripts_styles_comments(html):
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html,
                  flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html,
                  flags=re.DOTALL | re.IGNORECASE)
    return html


def check_tag_balance(html):
    """Вернуть список ошибок баланса тегов (пустой список = ОК).

    Учитывает void-элементы (br, img, input, ...) и самозакрывающиеся
    теги (<foo ... />), пропускает их без требования закрывающего тега.
    Кавычки в значениях атрибутов (одинарные/двойные) корректно
    экранируют '<'/'>' внутри себя от интерпретации как границ тега.
    """
    cleaned = _strip_scripts_styles_comments(html)
    n = len(cleaned)
    i = 0
    stack = []
    errors = []
    while i < n:
        if cleaned[i] != '<':
            i += 1
            continue
        j = i + 1
        if j < n and cleaned[j] == '!':
            k = cleaned.find('>', j)
            i = (k + 1) if k != -1 else n
            continue
        closing = False
        if j < n and cleaned[j] == '/':
            closing = True
            j += 1
        name_start = j
        while j < n and (cleaned[j].isalnum() or cleaned[j] in '-_'):
            j += 1
        name = cleaned[name_start:j].lower()
        if not name:
            i += 1
            continue
        in_quote = None
        while j < n:
            c2 = cleaned[j]
            if in_quote:
                if c2 == in_quote:
                    in_quote = None
            else:
                if c2 in ('"', "'"):
                    in_quote = c2
                elif c2 == '>':
                    break
            j += 1
        tag_end = j
        self_closing = False
        k = tag_end - 1
        while k > name_start and cleaned[k] in ' \t\r\n':
            k -= 1
        if k >= 0 and cleaned[k] == '/':
            self_closing = True
        if name not in _VOID_ELEMENTS and not self_closing:
            if not closing:
                stack.append((name, i))
            else:
                if not stack:
                    errors.append(
                        f"pos {i}: лишний закрывающий </{name}> без открытия")
                else:
                    top, top_pos = stack.pop()
                    if top != name:
                        errors.append(
                            f"pos {i}: несовпадение — открыт <{top}> "
                            f"(pos {top_pos}), закрыт </{name}>")
        i = tag_end + 1
    if stack:
        errors.append("незакрытые теги: " +
                      ", ".join(f"<{t}> (pos {p})" for t, p in stack))
    return errors


class FakeReg:
    def all(self): return []
    def get(self, d): return None


def make_client():
    state = _AppState(
        registry=FakeReg(), meters_repo=None,
        is_mqtt_connected=lambda: True,
        mqtt_message_count=lambda: 0, mqtt_error_count=lambda: 0,
        wb_db_client=None, consumption_service=None, started_at=0,
        aggregates_repo=None, aggregator=None)
    return create_app(state).test_client()


def test_root_serves_ui():
    c = make_client()
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.content_type
    html = r.get_data(as_text=True)
    # Ключевые маркеры UI
    assert 'x-data="app()"' in html, "Alpine root missing"
    assert "/api/status" in html, "status fetch missing"
    assert "toggleTheme" in html, "theme toggle missing"
    assert "loadConsumption" in html, "consumption loader missing"
    print("[OK] / serves Alpine UI")


def test_static_loader_reads_index():
    content = _load_static("index.html")
    assert "<!doctype html>" in content.lower()
    assert len(content) > 5000
    print("[OK] _load_static reads index.html")


def test_static_loader_fallback():
    # Несуществующий файл -> fallback, не исключение
    content = _load_static("does_not_exist_xyz.html")
    assert "wb-energy-meter" in content
    assert "/api/status" in content
    print("[OK] _load_static fallback works")


def test_docs_still_works():
    c = make_client()
    r = c.get("/api/docs")
    assert r.status_code == 200
    assert b"wb-energy-meter" in r.get_data()
    print("[OK] /api/docs still works")


def test_ui_has_dashboard_and_consumption():
    content = _load_static("index.html")
    # Вкладки
    assert "tab=='dash'" in content
    assert "tab=='consumption'" in content
    # Сводные плитки
    assert "Суммарная мощность" in content
    # Периоды
    assert "this_month" in content
    # Модалка деталей
    assert "openDetail" in content
    print("[OK] UI contains dashboard + consumption + detail")


def test_index_html_tag_balance():
    """Регрессия на незакрытый <template> (см. AGENTS.md) — самая дорогая
    ошибка в истории проекта: белый экран без единой ошибки в консоли."""
    content = _load_static("index.html")
    errors = check_tag_balance(content)
    assert not errors, (
        "Баланс тегов index.html нарушен:\n" + "\n".join(errors))
    print("[OK] баланс HTML-тегов index.html — 0 ошибок")


if __name__ == "__main__":
    test_root_serves_ui()
    test_static_loader_reads_index()
    test_static_loader_fallback()
    test_docs_still_works()
    test_ui_has_dashboard_and_consumption()
    test_index_html_tag_balance()
    print("\nВсе тесты Шага 6 (web UI) пройдены.")

"""Тесты Шага 74 (партия 15, этап 15.4): `docs/api-v2.md` не расходится с кодом.

Документ написан руками по коду и снабжён живыми примерами; чтобы он не устарел молча, тест
сверяет его с приложением в обе стороны:

  * каждый маршрут `/api/v2/*` приложения (метод + путь, без HEAD/OPTIONS) описан заголовком
    ``#### `МЕТОД /api/v2/путь/{имя}` ``, и каждый такой заголовок — настоящий маршрут;
  * «Указатель» в начале раздела 2 содержит ровно те же маршруты, что и заголовки;
  * каждый блок ```json в документе — корректный JSON (примеры не искажены при правке);
  * каждый код ошибки, которым код отвечает по `_err("…")`, есть в таблице 1.2, и каждый код
    таблицы 1.2 встречается в исходниках;
  * семь закрытых старых записывающих маршрутов (`legacy_guard`) перечислены в разделе 3.

Самостоятельный скрипт: python tests/test_step74_api_spec_sync.py
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, HERE)

from test_step49_acceptance_gaps import make_client  # noqa: E402
from wb_energy_meter import legacy_guard  # noqa: E402

DOC = os.path.join(REPO_ROOT, "docs", "api-v2.md")
HEADING = re.compile(r"^#### `(GET|POST|PUT|PATCH|DELETE) (/api/v2/\S+)`\s*$", re.M)
INDEX_ROW = re.compile(r"^\| (GET|POST|PUT|PATCH|DELETE) \| `(/api/v2/[^`]+)` \|", re.M)
ERR_ROW = re.compile(r"^\| (\d{3}) \| `([a-z0-9_]+)` \|", re.M)


def _doc() -> str:
    with open(DOC, encoding="utf-8") as f:
        return f.read()


def _app_routes(app) -> set:
    out = set()
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/v2/"):
            continue
        path = re.sub(r"<(?:[a-z_]+:)?([a-zA-Z_]+)>", r"{\1}", rule.rule)
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            out.add((method, path))
    return out


def test_routes_and_headings_match_both_ways():
    text = _doc()
    documented = set(HEADING.findall(text))
    assert len(documented) == len(HEADING.findall(text)), "один маршрут описан дважды"
    client, db, path, _registry = make_client(with_plans=True)
    try:
        app = client.application
        actual = _app_routes(app)
    finally:
        db.close()
        os.unlink(path)
    missing = sorted(actual - documented)
    extra = sorted(documented - actual)
    assert not missing, "маршруты есть в коде, но не описаны в docs/api-v2.md: %s" % missing
    assert not extra, "в docs/api-v2.md описаны несуществующие маршруты: %s" % extra
    assert len(actual) >= 80, "приложение отдало подозрительно мало маршрутов: %d" % len(actual)
    print("[OK] docs/api-v2.md ↔ приложение: %d маршрутов совпадают в обе стороны" % len(actual))


def test_index_table_equals_headings():
    text = _doc()
    index = INDEX_ROW.findall(text)
    headings = HEADING.findall(text)
    assert len(index) == len(set(index)), "в указателе повтор маршрута"
    assert set(index) == set(headings), (
        "указатель и заголовки расходятся: только в указателе %s, только в заголовках %s"
        % (sorted(set(index) - set(headings)), sorted(set(headings) - set(index))))
    print("[OK] указатель = заголовки (%d строк)" % len(index))


def test_json_examples_parse():
    text = _doc()
    blocks = re.findall(r"```json\n(.*?)```", text, re.S)
    assert len(blocks) >= 80, "в документе мало примеров JSON: %d" % len(blocks)
    for i, block in enumerate(blocks):
        try:
            json.loads(block)
        except ValueError as e:
            raise AssertionError("блок json №%d не разбирается: %s\n%s" % (i + 1, e, block[:300]))
    print("[OK] %d блоков json разбираются" % len(blocks))


def test_error_codes_documented_and_used():
    text = _doc()
    table = {code for _st, code in ERR_ROW.findall(text)}
    sources = ""
    for p in glob.glob(os.path.join(REPO_ROOT, "wb_energy_meter", "*.py")):
        with open(p, encoding="utf-8") as f:
            sources += f.read()
    used = set(re.findall(r'_err\(\s*"([a-z0-9_]+)"', sources))
    used.add(legacy_guard.CODE)
    assert not (used - table), "коды из кода не описаны в таблице 1.2: %s" % sorted(used - table)
    ghosts = [c for c in table if '"%s"' % c not in sources]
    assert not ghosts, "коды таблицы 1.2 нигде не встречаются в коде: %s" % ghosts
    assert len(table) >= 12, "таблица кодов ошибок неполная: %s" % sorted(table)
    print("[OK] коды ошибок: %d в таблице, все используются; все используемые описаны" % len(table))


def test_legacy_routes_listed():
    text = _doc()
    section = text.split("\n## 3. ", 1)[1].split("\n## 4. ", 1)[0]
    for method, _rx, _text, route in legacy_guard._ROUTE_HINTS:
        first = route.split(";")[0].strip()
        assert first.split(" ", 1)[1] in text, "маршрут замены %r не упомянут в документе" % first
    for method, pattern in (("POST", "/api/registry/meters"), ("PATCH", "/api/registry/meters/{device_id}/role"),
                            ("PATCH", "/api/registry/meters/{device_id}"),
                            ("DELETE", "/api/registry/meters/{device_id}"),
                            ("POST", "/api/registry/groups"), ("PATCH", "/api/registry/groups/{group_id}"),
                            ("DELETE", "/api/registry/groups/{group_id}")):
        assert "| %s | `%s` |" % (method, pattern) in section, "в разделе 3 нет строки %s %s" % (method, pattern)
    assert "moved_to_v2" in section and "кодом **3**" in section
    print("[OK] раздел 3 перечисляет все закрытые старые записывающие маршруты и код выхода CLI")


if __name__ == "__main__":
    test_routes_and_headings_match_both_ways()
    test_index_table_equals_headings()
    test_json_examples_parse()
    test_error_codes_documented_and_used()
    test_legacy_routes_listed()
    print("\nВсе тесты Шага 74 (docs/api-v2.md ↔ код) пройдены.")

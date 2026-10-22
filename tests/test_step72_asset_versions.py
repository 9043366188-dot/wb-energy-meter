"""Тесты партии 15, П14: адреса скриптов и стилей в отдаваемом index.html несут версию службы.

Проблема (docs/pilot-findings.md, П14): после обновления через API у пользователя остаётся открытой
старая страница; `/static/js/*.js` отдаются с `Cache-Control: public, max-age=43200`, адреса в
`index.html` без версии — обычная перезагрузка могла собрать новую разметку со старыми скриптами.

Решение: в исходнике `index.html` адреса остаются буквальными (`/static/js/core.js`), а маршрут `/`
дописывает `?v=<версия службы>` ко ВСЕМ своим `src`/`href` на `/static/…` (включая вендорные файлы:
они меняются только вместе с релизом, а правило одно для всех) и записывает версию в
`<meta name="wbem-build">` — по ней страница узнаёт, с какой версией загрузилась.

Самостоятельный скрипт (не pytest): python tests/test_step72_asset_versions.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import __version__
from wb_energy_meter.api import _AppState, build_selfcheck_result, create_app

STATIC = os.path.join(REPO_ROOT, "wb_energy_meter", "static")
REF_RE = re.compile(r'(?:src|href)="(/static/[^"]+)"')


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


def source_html():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()


def test_source_index_has_literal_urls():
    """Исходник не трогаем: адреса буквальные (самопроверка и тесты читают файл с диска)."""
    refs = REF_RE.findall(source_html())
    assert refs, "в index.html нет ссылок на /static/"
    assert not [r for r in refs if "?" in r], [r for r in refs if "?" in r]
    print("[OK] исходный index.html: адреса /static/ без ?v=")


def test_served_index_versions_every_static_url():
    html = make_client().get("/").get_data(as_text=True)
    refs = REF_RE.findall(html)
    src_refs = REF_RE.findall(source_html())
    assert len(refs) == len(src_refs) and len(refs) > 20, (len(refs), len(src_refs))
    suffix = "?v=" + __version__
    bad = [r for r in refs if not r.endswith(suffix)]
    assert not bad, "без версии: %s" % bad
    js = [r for r in refs if r.startswith("/static/js/")]
    assert len(js) >= 15 and all(r.endswith(".js" + suffix) for r in js), js
    vendor = [r for r in refs if r.startswith("/static/vendor/")]
    assert len(vendor) >= 4 and all(r.endswith(suffix) for r in vendor), vendor
    # тот же набор файлов, что в исходнике, — ничего не потеряно и не задвоено
    assert [r.split("?")[0] for r in refs] == src_refs
    print("[OK] отданный index.html: %d адресов /static/ (из них %d скриптов) с %s" % (len(refs), len(js), suffix))


def test_served_index_carries_build_meta():
    c = make_client()
    html = c.get("/").get_data(as_text=True)
    m = re.findall(r'<meta name="wbem-build" content="([^"]*)">', html)
    assert m == [__version__], m
    assert "__WBEM" not in html, "в отданной странице остались подстановочные метки"
    assert re.search(r'<meta name="wbem-build" content="">', source_html()), \
        "в исходнике метка должна быть пустой: версию подставляет сервер"
    print("[OK] <meta name=wbem-build> = версия службы (%s)" % __version__)


def test_versioned_urls_are_served():
    """Запрос с ?v= отдаёт тот же файл (Flask игнорирует query), в том числе вендорные файлы."""
    c = make_client()
    html = c.get("/").get_data(as_text=True)
    for ref in REF_RE.findall(html):
        r = c.get(ref)
        assert r.status_code == 200 and len(r.data) > 0, (ref, r.status_code)
    print("[OK] все адреса из отданной страницы отвечают 200")


def test_root_not_cached():
    r = make_client().get("/")
    cc = r.headers.get("Cache-Control", "")
    assert "no-cache" in cc or "no-store" in cc, cc
    print("[OK] index.html отдаётся без кэша (%s)" % cc)


def test_selfcheck_tolerates_versioned_urls():
    """Самопроверка разрешает путь без query: страница с ?v= не считается «файл не найден»."""
    with tempfile.TemporaryDirectory() as d:
        static, vendor = os.path.join(d, "static"), os.path.join(d, "vendor")
        os.makedirs(os.path.join(static, "js")); os.makedirs(vendor)
        with open(os.path.join(static, "js", "a.js"), "w") as f: f.write("1")
        with open(os.path.join(vendor, "v.js"), "w") as f: f.write("1")
        with open(os.path.join(static, "index.html"), "w") as f:
            f.write('<script src="/static/js/a.js?v=1.2.3"></script>'
                    '<script src="/static/vendor/v.js?v=1.2.3"></script>'
                    '<script src="/static/js/missing.js?v=1.2.3"></script>')
        res = build_selfcheck_result(static, vendor, "1.2.3")
        assert res["ok"] is False and res["checked"] == 3, res
        assert [x["ref"] for x in res["failed"]] == ["/static/js/missing.js?v=1.2.3"], res
        assert res["failed"][0]["reason"] == "файл не найден", res
    res = build_selfcheck_result(STATIC, os.path.join(STATIC, "vendor"), __version__)
    assert res["ok"] is True, res
    print("[OK] самопроверка: ?v= не мешает находить файл, отсутствующий файл по-прежнему виден")


if __name__ == "__main__":
    test_source_index_has_literal_urls()
    test_served_index_versions_every_static_url()
    test_served_index_carries_build_meta()
    test_versioned_urls_are_served()
    test_root_not_cached()
    test_selfcheck_tolerates_versioned_urls()
    print("[ALL OK] test_step72_asset_versions")

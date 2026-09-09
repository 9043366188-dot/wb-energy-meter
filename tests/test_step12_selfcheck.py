"""Тесты Шага 12: самопроверка интерфейса и заглушка вместо белого экрана
(ТЗ v0.11.1). Самостоятельный скрипт (не pytest), запускается
`python3 tests/test_step12_selfcheck.py`.

09.09.2026 обновление до v0.11.0 положило контроллер: /static/vendor/<file>
падал по несовместимости с Werkzeug 1.0.1, alpine.min.js не грузился,
Alpine не стартовал вообще — белый экран, а /health при этом отвечал
"всё хорошо" (см. AGENTS.md). Этот файл проверяет обе части починки:
GET /api/selfcheck (задача 1) и HTML/JS-заглушку в index.html (задача 2).

Тесты на "сломанные" сценарии (отсутствующий/пустой файл, нечитаемый
index.html) прогоняются на build_selfcheck_result() с временным
каталогом — НЕ на реальном wb_energy_meter/static: настоящие вендоренные
файлы репозитория при этом не трогаются вообще. Отдельно, целиком (не
только логика) через Flask test_client() проверяется реальный
/api/selfcheck на настоящих файлах репозитория (тест 1) — это и есть
интеграционная проверка "той же логики, что и у маршрута статики".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.api import build_selfcheck_result, create_app, _AppState
from wb_energy_meter import __version__


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


# ---------------------------------------------------------------------
# 1. /api/selfcheck на целых файлах (реальный репозиторий) -> ok:true
# ---------------------------------------------------------------------

def test_selfcheck_ok_on_real_static():
    c = make_client()
    r = c.get("/api/selfcheck")
    assert r.status_code == 200, r.status_code
    data = r.get_json()
    assert data["ok"] is True, data
    assert data["failed"] == [], data
    assert data["checked"] > 0, "не нашлось ни одной /static/ ссылки — проверка бессмысленна"
    assert data["version"] == __version__
    print("[OK] /api/selfcheck на целых файлах реального репозитория -> ok:true, checked=%d"
          % data["checked"])


# ---------------------------------------------------------------------
# Вспомогательное: собрать временный static/ с index.html и vendor/
# ---------------------------------------------------------------------

def _make_tmp_static(tmp, refs_and_content):
    """refs_and_content: список (относительный путь внутри vendor/, содержимое
    или None чтобы не создавать файл вовсе). Возвращает (static_dir, vendor_dir)."""
    static_dir = os.path.join(tmp, "static")
    vendor_dir = os.path.join(static_dir, "vendor")
    os.makedirs(vendor_dir, exist_ok=True)
    tags = []
    for rel, content in refs_and_content:
        path = os.path.join(vendor_dir, rel)
        tags.append('<script src="/static/vendor/%s"></script>' % rel)
        if content is not None:
            with open(path, "wb") as f:
                f.write(content)
    html = "<!doctype html><html><head></head><body>%s</body></html>" % "\n".join(tags)
    with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return static_dir, vendor_dir


# ---------------------------------------------------------------------
# 2. Один вендоренный файл отсутствует/пуст -> ok:false, он в failed,
#    "код ответа" (тут — сама функция) не бросает исключений
# ---------------------------------------------------------------------

def test_selfcheck_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        static_dir, vendor_dir = _make_tmp_static(tmp, [
            ("alpine.min.js", b"var x=1;"),
            ("leaflet.js", None),  # файл не создан вовсе
        ])
        result = build_selfcheck_result(static_dir, vendor_dir, "0.11.1")
        assert result["ok"] is False, result
        assert result["checked"] == 2, result
        refs_failed = {item["ref"] for item in result["failed"]}
        assert "/static/vendor/leaflet.js" in refs_failed, result
        assert "/static/vendor/alpine.min.js" not in refs_failed, result
        reason = next(i["reason"] for i in result["failed"]
                      if i["ref"] == "/static/vendor/leaflet.js")
        assert "не найден" in reason, reason
        print("[OK] selfcheck: отсутствующий вендоренный файл -> ok:false, в failed")


def test_selfcheck_empty_file():
    with tempfile.TemporaryDirectory() as tmp:
        static_dir, vendor_dir = _make_tmp_static(tmp, [
            ("alpine.min.js", b""),  # файл существует, но пустой
        ])
        result = build_selfcheck_result(static_dir, vendor_dir, "0.11.1")
        assert result["ok"] is False, result
        assert result["checked"] == 1, result
        assert result["failed"][0]["ref"] == "/static/vendor/alpine.min.js"
        assert "пуст" in result["failed"][0]["reason"], result
        print("[OK] selfcheck: пустой вендоренный файл -> ok:false, в failed")


# ---------------------------------------------------------------------
# 3. index.html нечитаем -> ok:false, без исключения (никаких HTTP-кодов
#    тут нет — build_selfcheck_result всегда возвращает словарь, а не
#    бросает; маршрут поверх него всегда отвечает 200, это уже проверено
#    в test_selfcheck_ok_on_real_static на реальном приложении)
# ---------------------------------------------------------------------

def test_selfcheck_unreadable_index():
    with tempfile.TemporaryDirectory() as tmp:
        static_dir = os.path.join(tmp, "static")
        vendor_dir = os.path.join(static_dir, "vendor")
        os.makedirs(vendor_dir, exist_ok=True)
        # index.html вообще не существует -> OSError при open()
        result = build_selfcheck_result(static_dir, vendor_dir, "0.11.1")
        assert result["ok"] is False, result
        assert result["checked"] == 0, result
        assert result["failed"][0]["ref"] == "index.html", result
        assert "version" in result and result["version"] == "0.11.1"
        print("[OK] selfcheck: index.html не существует -> ok:false, без исключения")


def test_selfcheck_index_bad_encoding():
    with tempfile.TemporaryDirectory() as tmp:
        static_dir = os.path.join(tmp, "static")
        vendor_dir = os.path.join(static_dir, "vendor")
        os.makedirs(vendor_dir, exist_ok=True)
        # Невалидный UTF-8 -> UnicodeDecodeError при чтении, тоже не должно
        # долетать наружу исключением.
        with open(os.path.join(static_dir, "index.html"), "wb") as f:
            f.write(b"\xff\xfe\x00broken")
        result = build_selfcheck_result(static_dir, vendor_dir, "0.11.1")
        assert result["ok"] is False, result
        assert result["failed"][0]["ref"] == "index.html", result
        print("[OK] selfcheck: index.html в битой кодировке -> ok:false, без исключения")


# ---------------------------------------------------------------------
# 4. Обход каталога через ref из index.html не должен пройти (защита той
#    же логикой, что и у маршрута статики)
# ---------------------------------------------------------------------

def test_selfcheck_rejects_path_traversal_ref():
    with tempfile.TemporaryDirectory() as tmp:
        static_dir = os.path.join(tmp, "static")
        vendor_dir = os.path.join(static_dir, "vendor")
        os.makedirs(vendor_dir, exist_ok=True)
        html = ('<!doctype html><html><body>'
                '<script src="/static/vendor/../../etc/passwd"></script>'
                '</body></html>')
        with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        result = build_selfcheck_result(static_dir, vendor_dir, "0.11.1")
        assert result["ok"] is False, result
        assert result["failed"][0]["reason"] in (
            "недопустимый путь", "путь вне каталога static"), result
        print("[OK] selfcheck: обход каталога в ref -> отказ, не 500")


# ---------------------------------------------------------------------
# 5. Заглушка есть в index.html, содержит подсказку с journalctl, и в
#    ней нет ни одной директивы Alpine — иначе она не сработает именно
#    тогда, когда Alpine не загрузился.
# ---------------------------------------------------------------------

def _extract_boot_error_block(html):
    start = html.index('id="boot-error"')
    # Блок целиком — от открывающего <div ...id="boot-error"...> до
    # закрывающего <script> с window.__wbemBootOk (заведомо избыточная,
    # но простая граница: обе части заглушки описаны в §3 ТЗ рядом).
    end_marker = "__wbemBootOk"
    end = html.index(end_marker, start)
    # Дотягиваем до конца следующего </script>, чтобы не обрезать код.
    end = html.index("</script>", end) + len("</script>")
    # А начало — с начала строки div, несколько назад, чтобы взять и div,
    # и <script> ловушки ошибок целиком.
    div_start = html.rindex("<div", 0, start)
    return html[div_start:end]


def test_index_html_has_boot_stub_without_alpine_directives():
    index_path = os.path.join(REPO_ROOT, "wb_energy_meter", "static", "index.html")
    with open(index_path, encoding="utf-8") as f:
        html = f.read()

    assert 'id="boot-error"' in html, "заглушка (div#boot-error) не найдена в index.html"
    block = _extract_boot_error_block(html)

    assert "journalctl" in block, "в заглушке нет подсказки с journalctl"
    assert "curl" in block, "в заглушке нет подсказки с curl"

    forbidden = ["x-show", "x-if", "x-text", "x-data", "x-init", "x-for", "x-model"]
    for directive in forbidden:
        assert directive not in block, (
            "заглушка использует директиву Alpine %r — она не сработает "
            "именно тогда, когда Alpine не загрузился" % directive)

    # Заглушка обязана прятаться после успешного старта Alpine.
    assert "window.__wbemBootOk" in html
    assert re.search(r"init\(\)\s*\{\s*(?://[^\n]*\n\s*)*if\(window\.__wbemBootOk\)",
                      html), (
        "init() не вызывает window.__wbemBootOk() первым делом — "
        "заглушка не будет снята при успешном старте")
    print("[OK] заглушка в index.html: есть, без директив Alpine, содержит journalctl/curl")


def test_boot_stub_not_shown_immediately():
    """Заглушка не должна мигать при нормальной загрузке (§3 ТЗ) — значит
    видимость выставляется НЕ инлайновым style="display:block" сразу, а
    через setTimeout (1.5с) внутри <script>."""
    index_path = os.path.join(REPO_ROOT, "wb_energy_meter", "static", "index.html")
    with open(index_path, encoding="utf-8") as f:
        html = f.read()
    div_start = html.index('id="boot-error"')
    div_tag_end = html.index(">", div_start)
    div_open_tag = html[html.rindex("<div", 0, div_start):div_tag_end]
    assert "display:none" in div_open_tag.replace(" ", ""), (
        "div#boot-error обязан быть скрыт по умолчанию инлайн-стилем "
        "(display:none), иначе он мелькнёт до setTimeout")
    assert "setTimeout" in html
    assert re.search(r"setTimeout\([^,]+,\s*1500\)", html), (
        "не нашёлся setTimeout(..., 1500) — заглушка должна показываться "
        "с задержкой 1.5с, а не сразу")
    print("[OK] заглушка скрыта по умолчанию и показывается по таймеру 1.5с")


# ---------------------------------------------------------------------
# 6. bash -n scripts/self-update.sh проходит
# ---------------------------------------------------------------------

def test_self_update_sh_syntax():
    path = os.path.join(REPO_ROOT, "scripts", "self-update.sh")
    r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    assert r.returncode == 0, "bash -n scripts/self-update.sh:\n" + r.stderr
    print("[OK] bash -n scripts/self-update.sh — синтаксис чист")


def test_self_update_sh_checks_selfcheck():
    """Регрессия: verify_after_install() обязан звать самопроверку
    интерфейса (§2.2 ТЗ), а не только wait_for_health."""
    path = os.path.join(REPO_ROOT, "scripts", "self-update.sh")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    assert "api/selfcheck" in content, "self-update.sh не обращается к /api/selfcheck"
    assert "404" in content, "self-update.sh не учитывает случай 404 (старая версия без эндпоинта)"
    assert "json.load" in content, "разбор ответа selfcheck должен идти через json.load (без jq)"
    # jq на контроллере может не быть — разбор JSON обязан идти через python3,
    # а не через вызов утилиты jq. Проверяем отсутствие реального вызова "jq "
    # как команды (упоминания в комментариях о том, что jq НЕ используется, —
    # это нормально и не должно ловиться проверкой).
    real_calls = [line for line in content.splitlines()
                  if re.search(r"(^|[|;&]|\s)jq(\s|$)", line.split("#", 1)[0])]
    assert not real_calls, "похоже на вызов jq как команды: %r" % real_calls
    print("[OK] self-update.sh: /api/selfcheck подключён, 404 не считается провалом, разбор без jq")


if __name__ == "__main__":
    test_selfcheck_ok_on_real_static()
    test_selfcheck_missing_file()
    test_selfcheck_empty_file()
    test_selfcheck_unreadable_index()
    test_selfcheck_index_bad_encoding()
    test_selfcheck_rejects_path_traversal_ref()
    test_index_html_has_boot_stub_without_alpine_directives()
    test_boot_stub_not_shown_immediately()
    test_self_update_sh_syntax()
    test_self_update_sh_checks_selfcheck()
    print("\nВсе тесты Шага 12 (самопроверка интерфейса) пройдены.")

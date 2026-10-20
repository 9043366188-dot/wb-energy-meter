"""Партия 13, 13.4 (A33, §8.5): состояния экранов и устаревшие ответы — настоящий
Chromium, настоящее Flask-приложение.

Экраны: «Обзор», «Отчёты», карточка «Структуры», карточка узла «Плана v3», поиск
«Структуры». Состояния: loading / empty / ready / partial / error; при кратком обрыве
остаются ПРЕЖНИЕ данные с признаком «устарело, обновлено ЧЧ:ММ»; запоздавший ответ
прежнего запроса не перетирает новый (A33).

Обрывы и задержки — обёртка над fetch из fetch_tools.py (ставится ДО скриптов приложения):
`window.__fail`, `window.__delay`, `window.__delayOnce`. Это не `page.route`: обрыв через сеть
Chromium пишет «Failed to load resource» в консоль, а сценарий проверяет, что приложение само
не бросает исключений.

Запуск: python tests/browser/test_b20_states.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from demo_seed import seed_demo  # noqa: E402
from fetch_tools import FETCH_WRAP  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
STALE_RE = re.compile(r"устарело, обновлено \d\d:\d\d")
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def vis(page, testid: str, scope: str = "") -> bool:
    loc = page.locator(f"{scope} [data-testid={testid}]".strip())
    return loc.count() > 0 and loc.first.is_visible()


def text(page, testid: str, scope: str = "") -> str:
    loc = page.locator(f"{scope} [data-testid={testid}]".strip())
    return loc.first.inner_text().strip() if loc.count() else ""


def js(page, code: str):
    return page.evaluate(code)


def fire(page, code: str) -> None:
    """Запустить асинхронный вызов приложения и не ждать его (evaluate ждёт промис)."""
    page.evaluate("(()=>{ " + code + "; })()")


def wait_for(page, expr: str, ms: int = 6000) -> bool:
    """Дождаться условия в странице (вместо фиксированной паузы: на медленной машине ответ
    приходит позже, а общий опрос раз в 5 с может перетереть подставленное значение)."""
    try:
        page.wait_for_function(expr, timeout=ms)
        return True
    except Exception:
        return False


def pw_errors(bp) -> list[str]:
    """Исключения приложения (pageerror) и ошибки консоли, кроме сетевых сообщений Chromium."""
    return [e for e in bp.errors if "Failed to load resource" not in e]


def main() -> int:
    with Harness() as h:
        info = seed_demo(h)
        bp = open_browser(h.base_url, init_script=FETCH_WRAP)
        page = bp.page
        page.wait_for_timeout(1200)
        js(page, APP + ".toggleUiSet()")
        page.wait_for_timeout(500)
        pt_light = info["points"]["dev-light"]["id"]
        pt_sock = info["points"]["dev-sock"]["id"]
        shr1 = info["shr1"]["id"]
        shr2 = info["shr2"]["id"]

        # ===== 1. Обзор: ready → обрыв (устарело, цифры остались) → восстановление =====
        page.locator("nav button:visible", has_text="Обзор").click()
        page.wait_for_timeout(1500)
        check(js(page, APP + ".feedStatus('ov-snap')") in ("ready", "partial"),
              "Обзор: после загрузки лента 'ov-snap' — ready/partial")
        check(not vis(page, "ov-stale"), "Обзор: свежие данные — признака «устарело» нет")
        kw_before = page.locator(".tiles .tile .val").first.inner_text().strip()
        check(kw_before not in ("", "—"), f"Обзор: плитка мощности показывает число ({kw_before!r})")

        js(page, "window.__fail=['/api/v2/snapshot']")
        js(page, APP + ".loadOverviewV2Snapshot()")
        page.wait_for_timeout(300)
        check(vis(page, "ov-stale") and STALE_RE.search(text(page, "ov-stale")) is not None,
              f"Обзор: при обрыве — «устарело, обновлено ЧЧ:ММ» ({text(page, 'ov-stale')!r})")
        check(page.locator(".tiles .tile .val").first.inner_text().strip() == kw_before,
              "Обзор: при обрыве прежние цифры остались на экране")
        check(vis(page, "ov-snap-error"), "Обзор: сообщение об ошибке рядом с прежними данными")
        js(page, "window.__fail=[]")
        js(page, APP + ".loadOverviewV2Snapshot()")
        page.wait_for_timeout(500)
        check(not vis(page, "ov-stale") and not vis(page, "ov-snap-error"),
              "Обзор: после восстановления признак «устарело» и ошибка исчезли")

        # «после таймаута данные не изображаются текущими»: ответа нет дольше FEED_STALE_MS
        shown = js(page, """(async()=>{
            const a=%s; const f=a.feeds['ov-snap'];
            a.feeds={...a.feeds,'ov-snap':{...f,updatedAt:Date.now()-60000}}; a.feedNow=Date.now();
            await a.$nextTick();
            const el=document.querySelector('[data-testid=ov-stale]');
            return !!el && el.offsetParent!==null;
        })()""" % APP)
        check(shown is True, "Обзор: нет свежего ответа дольше порога — данные помечены устаревшими")
        js(page, APP + ".loadOverviewV2Snapshot()")
        page.wait_for_timeout(500)

        # частично: у точки нет мощности
        # подмена и проверка — в одном тике страницы: общий опрос (раз в 5 с) не успевает её перетереть
        partial = js(page, """(async()=>{ const a=%s; const s=JSON.parse(JSON.stringify(a.overviewV2Snapshot));
            s.points[0].power_w=null; a.overviewV2Snapshot=s; await a.$nextTick();
            const el=document.querySelector('[data-testid=ov-partial]');
            return {text:a.overviewV2PartialText(), shown:!!el && el.offsetParent!==null,
                    dom: el ? el.textContent.trim() : ''}; })()""" % APP)
        check(bool(partial["text"]) and partial["shown"] and "неполные" in partial["dom"],
              f"Обзор: partial — «{partial['text']}»")
        js(page, APP + ".loadOverviewV2Snapshot()")
        page.wait_for_timeout(500)

        # ===== 2. Отчёты: empty → ready → обрыв (устарело) → смена среза сбрасывает =====
        page.locator("nav button:visible", has_text="Отчёты").click()
        page.wait_for_timeout(1200)
        check(vis(page, "rv2-empty"), "Отчёты: до первого запроса — подсказка, что делать дальше")
        d = js(page, APP + "._ovDefaultDates()")
        js(page, """(()=>{ const a=%s; a.rv2Dimension='point'; a.rv2ScopeIds=[%d]; a.rv2From=%r; a.rv2To=%r; })()"""
           % (APP, pt_light, d["from"], d["to"]))
        js(page, APP + ".runRv2Query()")
        page.wait_for_timeout(800)
        check(js(page, APP + ".feedStatus('rv2-query')") in ("ready", "partial") and js(page, APP + ".rv2Result !== null"),
              "Отчёты: ready/partial после запроса")
        check(not vis(page, "rv2-stale"), "Отчёты: свежий отчёт без признака «устарело»")

        js(page, "window.__fail=['/api/v2/reports/query']")
        js(page, APP + ".runRv2Query()")
        page.wait_for_timeout(500)
        check(vis(page, "rv2-stale") and STALE_RE.search(text(page, "rv2-stale")) is not None,
              "Отчёты: тот же запрос не ответил — прежний отчёт с «устарело, обновлено ЧЧ:ММ»")
        check(js(page, APP + ".rv2Result !== null") and vis(page, "rv2-error"),
              "Отчёты: прежний отчёт остался, ошибка показана рядом")
        # другой период: прежний отчёт ЧУЖОЙ — выдавать за текущий нельзя
        js(page, """(()=>{ const a=%s; a.rv2From=a.rv2From.slice(0,8)+'01'; a.rv2To=a.rv2From; })()""" % APP)
        js(page, APP + ".runRv2Query()")
        page.wait_for_timeout(500)
        check(js(page, APP + ".rv2Result === null"),
              "Отчёты: запрос за другой период не ответил — прежний отчёт за иной период не выдаётся за текущий")
        js(page, "window.__fail=[]")

        # период без данных: empty со следующим действием
        js(page, """(()=>{ const a=%s; a.rv2From='2001-01-01'; a.rv2To='2001-01-31'; })()""" % APP)
        js(page, APP + ".runRv2Query()")
        page.wait_for_timeout(800)
        check(js(page, APP + ".feedStatus('rv2-query')") in ("empty", "partial", "ready")
              and (vis(page, "rv2-no-rows") or js(page, APP + ".rv2Result") is not None),
              "Отчёты: период без данных — отчёт построен, состояние показано")

        # A33: запоздалый ответ прежнего запроса не перетирает новый
        js(page, """(()=>{ const a=%s; a.rv2From=%r; a.rv2To=%r; a.rv2ScopeIds=[%d]; })()"""
           % (APP, d["from"], d["to"], pt_light))
        js(page, "window.__delayOnce['/api/v2/reports/query']=1500")
        fire(page, APP + ".runRv2Query()")
        page.wait_for_timeout(100)
        js(page, "%s.rv2ScopeIds=[%d]" % (APP, pt_sock))
        fire(page, APP + ".runRv2Query()")
        page.wait_for_timeout(2300)
        ids = js(page, "((%s.rv2Result||{rows:[]}).rows||[]).map(r=>r.id)" % APP)
        check(ids == [pt_sock],
              f"Отчёты (A33): запоздавший ответ по «Освещению» не перетёр новый по «Розеткам» ({ids})")

        # смена среза убирает результат прежнего среза
        js(page, "(()=>{ const a=%s; a.rv2Dimension='group'; a.rv2ResetResult(); })()" % APP)
        page.wait_for_timeout(200)
        check(js(page, APP + ".rv2Result === null"), "Отчёты: смена среза убирает отчёт прежнего среза")

        # ===== 3. Структура: карточка узла — ready, устарело, частично, ошибка + «Повторить» =====
        page.locator("nav button:visible", has_text="Структура").click()
        page.wait_for_timeout(1500)
        S = "[data-drawer=struct]"
        fire(page, APP + ".openInspector('node', %d)" % shr1)
        page.wait_for_timeout(1200)
        check(page.locator(S + " h3").count() > 0,
              "Структура: карточка узла загружена")
        check(js(page, APP + ".feedStatus('inspector')") == "ready", "Структура: карточка — ready")
        check(not vis(page, "card-stale", S) and not vis(page, "card-partial", S),
              "Структура: у свежей карточки нет пометок «устарело»/«частично»")

        js(page, "window.__fail=['/api/v2/snapshot']")
        js(page, APP + "._pollTick()")
        wait_for(page, "()=>{ const e=document.querySelector('[data-drawer=struct] [data-testid=card-stale]');"
                       " return !!e && e.offsetParent!==null; }")
        check(vis(page, "card-stale", S) and STALE_RE.search(text(page, "card-stale", S)) is not None,
              f"Структура: живые числа карточки не обновились — «устарело, обновлено ЧЧ:ММ» ({text(page, 'card-stale', S)!r})")
        js(page, "window.__fail=[]")
        js(page, APP + "._pollTick()")
        page.wait_for_timeout(700)
        check(not vis(page, "card-stale", S), "Структура: после восстановления опроса признак «устарело» ушёл")

        js(page, "window.__fail=['/lines']")
        fire(page, APP + ".openInspector('node', %d)" % shr2)
        page.wait_for_timeout(1000)
        check(vis(page, "card-partial", S) and "Линии" in text(page, "card-partial", S),
              f"Структура: partial — «{text(page, 'card-partial', S)}»")
        check(js(page, APP + ".feedStatus('inspector')") == "partial", "Структура: лента карточки — partial")
        js(page, "window.__fail=[]")

        js(page, "window.__fail=['/api/v2/topology/nodes/%d']" % shr1)
        fire(page, APP + ".openInspector('node', %d)" % shr1)
        page.wait_for_timeout(800)
        check(vis(page, "card-error", S) and vis(page, "card-retry", S),
              "Структура: error — сообщение и кнопка «Повторить»")
        js(page, "window.__fail=[]")
        page.locator(S + " [data-testid=card-retry]").click()
        page.wait_for_timeout(1200)
        check(not vis(page, "card-error", S) and js(page, APP + ".feedStatus('inspector')") == "ready",
              "Структура: «Повторить» загружает карточку")

        # A33: ответ ранее выбранной карточки не перетирает новую
        js(page, "window.__delayOnce['/api/v2/topology/nodes/%d']=1500" % shr1)
        fire(page, APP + ".openInspector('node', %d)" % shr1)
        page.wait_for_timeout(100)
        fire(page, APP + ".openInspector('node', %d)" % shr2)
        page.wait_for_timeout(2300)
        cur = js(page, "(()=>{ const i=%s.structInspector; return i && [i.id, (i.data||{}).name]; })()" % APP)
        check(cur and cur[0] == shr2 and cur[1] == info["shr2"]["name"],
              f"Структура (A33): запоздавший ответ ЩР-1 не перетёр карточку ЩР-2 ({cur})")

        # закрытие во время загрузки: запоздалый ответ карточку заново не открывает
        js(page, "window.__delayOnce['/api/v2/topology/nodes/%d']=1000" % shr1)
        fire(page, APP + ".openInspector('node', %d)" % shr1)
        page.wait_for_timeout(100)
        js(page, APP + ".closeInspector()")
        page.wait_for_timeout(1800)
        check(js(page, APP + ".structInspector === null && !" + APP + ".structInspectorLoading"),
              "Структура: закрыли карточку до ответа — она не открылась заново")

        # ===== 4. Структура: поиск — error и empty =====
        page.fill("[data-testid=struct-search-input]", "освещ")
        page.wait_for_timeout(900)
        check(vis(page, "struct-search-results"), "Структура: поиск «освещ» показывает результаты")
        js(page, "window.__fail=['/api/v2/structure/points']")
        page.fill("[data-testid=struct-search-input]", "освещени")
        page.wait_for_timeout(900)
        check(vis(page, "struct-search-error") and "не ответил" in text(page, "struct-search-error"),
              "Структура: поиск не ответил — сказано словами, экран жив")
        js(page, "window.__fail=[]")
        page.fill("[data-testid=struct-search-input]", "нет-такого-xyz-123")
        page.wait_for_timeout(900)
        check(vis(page, "struct-search-empty") and "Ничего не найдено" in text(page, "struct-search-empty"),
              "Структура: пустой поиск объясняет, что делать дальше")
        page.fill("[data-testid=struct-search-input]", "")
        page.wait_for_timeout(600)

        # ===== 5. План v3: карточка узла — тот же признак «устарело» =====
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(1800)
        P = "[data-drawer=plan]"
        fire(page, APP + ".openInspectorInPlace('node', %d)" % shr1)
        page.wait_for_timeout(1200)
        check(js(page, APP + ".feedStatus('inspector')") == "ready" and not vis(page, "card-stale", P),
              "План v3: карточка узла загружена, пометки «устарело» нет")
        js(page, "window.__fail=['/api/v2/snapshot']")
        js(page, APP + "._pollTick()")
        page.wait_for_timeout(500)
        check(vis(page, "card-stale", P) and STALE_RE.search(text(page, "card-stale", P)) is not None,
              "План v3: живые числа карточки узла не обновились — «устарело, обновлено ЧЧ:ММ»")
        js(page, "window.__fail=[]")
        js(page, APP + "._pollTick()")
        page.wait_for_timeout(700)
        check(not vis(page, "card-stale", P), "План v3: после восстановления «устарело» ушло")

        js(page, "window.__fail=['/api/v2/topology/nodes/%d']" % shr1)
        fire(page, APP + ".openInspectorInPlace('node', %d)" % shr1)
        page.wait_for_timeout(800)
        check(vis(page, "card-error", P) and vis(page, "card-retry", P), "План v3: error с кнопкой «Повторить»")
        js(page, "window.__fail=[]")
        page.locator(P + " [data-testid=card-retry]").click()
        page.wait_for_timeout(1200)
        check(not vis(page, "card-error", P), "План v3: «Повторить» загружает карточку")

        check(not pw_errors(bp), f"исключений и ошибок консоли приложения нет ({pw_errors(bp)[:3]})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

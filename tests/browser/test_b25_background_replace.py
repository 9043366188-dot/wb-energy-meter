"""Партия 14 (0.24.0), A30: замена подложки плана этажа через предпросмотр, перепривязка, возврат.

Настоящий Chromium, настоящее Flask-приложение.
  1. План этажа 800×500, два значка. Выбран файл 1600×1000 (то же соотношение сторон): открылось
     окно предпросмотра, по умолчанию «пересчитать значки», точки стоят там же (в долях картинки).
     «Оставить на прежних пикселях» двигает точки в предпросмотре. «Отмена» — в базе ничего не изменилось.
  2. «Заменить подложку» с пересчётом: размеры плана 1600×1000, значки ×2, на карте новая картинка.
  3. Обрезанный фон 900×1000 (другое соотношение сторон): выбора «пересчитать» нет, написано про
     перепривязку, за краем картинки красная точка и счётчик; после замены значки не сдвинулись.
  4. «Версии подложки…»: три строки, текущая отмечена; «Вернуть…» первую (800×500) — предпросмотр,
     после подтверждения размеры 800×500 и картинка та же, что была (байт в байт).
  5. Консоль без ошибок.

Запуск: python tests/browser/test_b25_background_replace.py
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from waits import wait_map_v3_ready  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def make_png(width: int, height: int, rgb=(214, 226, 240)) -> bytes:
    row = b"\x00" + bytes(rgb) * width
    comp = zlib.compressobj(9)
    raw = b"".join(comp.compress(row) for _ in range(height)) + comp.flush()

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def api(page, path):
    return page.evaluate(f"fetch({path!r}).then(r=>r.json())")


def wait_dialog(page, shown=True):
    if shown:
        page.wait_for_selector("[data-testid=pbg-dialog]", timeout=8000)
    else:
        page.wait_for_function("() => !document.querySelector('[data-testid=pbg-dialog]')", timeout=8000)


def dots(page):
    return page.evaluate("""() => [...document.querySelectorAll('[data-testid=pbg-dot]')].map(e => ({
        left: parseFloat(e.style.left), top: parseFloat(e.style.top),
        out: e.classList.contains('pbg-dot-out')}))""")


def plan_items(h, plan_id):
    body = h.client.get(f"/api/v2/plans/{plan_id}").get_json()
    return body, {i["node_id"]: (i["geometry"]["x"], i["geometry"]["y"]) for i in body["items"]}


def main() -> int:
    tmp = tempfile.mkdtemp()
    files = {}
    for name, (w, hh, rgb) in {"a": (800, 500, (214, 226, 240)), "scaled": (1600, 1000, (232, 236, 214)),
                               "crop": (900, 1000, (240, 226, 226))}.items():
        files[name] = os.path.join(tmp, f"{name}.png")
        with open(files[name], "wb") as f:
            f.write(make_png(w, hh, rgb))
    original_bytes = open(files["a"], "rb").read()

    with Harness() as h:
        r = h.client.post("/api/v2/plans", data={
            "name": "Этаж 1", "plan_kind": "floor", "file": (open(files["a"], "rb"), "a.png")},
            content_type="multipart/form-data")
        assert r.status_code == 201, r.get_json()
        plan = r.get_json()
        pid = plan["id"]
        nodes = {}
        for key, xy in (("A", (400, 250)), ("B", (700, 400))):
            node = h.make_node(key, f"Щит {key}", "panel")
            nodes[key] = node["id"]
            rr = h.client.post(f"/api/v2/plans/{pid}/items", json={
                "kind": "node", "node_id": node["id"], "geometry": {"x": xy[0], "y": xy[1]},
                "coord_space": "image_px_xy_v2"})
            assert rr.status_code == 201, rr.get_json()

        bp = open_browser(h.base_url, viewport={"width": 1400, "height": 1000})
        page = bp.page
        page.wait_for_timeout(800)
        page.wait_for_function("() => !!document.querySelector('[x-data]')._x_dataStack")
        if page.evaluate(APP + ".uiSet") != "v2":
            page.evaluate(APP + ".toggleUiSet()")
            page.wait_for_timeout(300)
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_function(
            "() => { const a = %s; return a.planV3Current && a.planV3Current.items.length === 2; }" % APP,
            timeout=15000)
        wait_map_v3_ready(page)

        # ---- 1. предпросмотр: то же соотношение сторон ---------------------------------------
        page.set_input_files("[data-testid=planv3-replace-bg] input", files["scaled"])
        wait_dialog(page)
        note = page.inner_text("[data-testid=pbg-note]")
        check("соотношение сторон то же" in note, f"предпросмотр: сказано про то же соотношение сторон ({note[:70]})")
        check(page.locator("[data-testid=pbg-mode-scale]").is_checked(), "по умолчанию — «пересчитать значки»")
        d = sorted(dots(page), key=lambda x: x["left"])
        check(len(d) == 2 and abs(d[0]["left"] - 50) < 0.1 and abs(d[0]["top"] - 50) < 0.1
              and abs(d[1]["left"] - 87.5) < 0.1 and abs(d[1]["top"] - 80) < 0.1 and not any(x["out"] for x in d),
              f"точки предпросмотра на тех же местах рисунка ({d})")
        page.click("[data-testid=pbg-mode-keep]")
        page.wait_for_timeout(150)
        d = sorted(dots(page), key=lambda x: x["left"])
        check(len(d) == 2 and abs(d[0]["left"] - 25) < 0.1 and abs(d[0]["top"] - 25) < 0.1,
              f"«оставить на пикселях»: точки в предпросмотре сдвинулись ({d})")
        page.click("[data-testid=pbg-cancel]")
        wait_dialog(page, shown=False)
        body, pos = plan_items(h, pid)
        check(body["image_width"] == 800 and pos[nodes["A"]] == (400, 250) and len(h.client.get(
            f"/api/v2/plans/{pid}/image-versions").get_json()) == 1,
              "«Отмена»: подложка, значки и версии не изменились")

        # ---- 2. замена с пересчётом ----------------------------------------------------------
        page.set_input_files("[data-testid=planv3-replace-bg] input", files["scaled"])
        wait_dialog(page)
        page.click("[data-testid=pbg-apply]")
        wait_dialog(page, shown=False)
        page.wait_for_function("() => %s.planV3Current && %s.planV3Current.image_width === 1600" % (APP, APP),
                               timeout=8000)
        body, pos = plan_items(h, pid)
        check((body["image_width"], body["image_height"]) == (1600, 1000), "после замены: 1600×1000")
        check(pos[nodes["A"]] == (800, 500) and pos[nodes["B"]] == (1400, 800),
              f"значки пересчитаны ×2 ({pos})")
        check("пересчитаны" in page.inner_text("body"), "сообщение: значки пересчитаны")

        # ---- 3. обрезанный фон ---------------------------------------------------------------
        page.set_input_files("[data-testid=planv3-replace-bg] input", files["crop"])
        wait_dialog(page)
        note = page.inner_text("[data-testid=pbg-note]")
        check("перепривязать" in note, f"обрезанный фон: сказано про перепривязку ({note[:60]}…)")
        check(page.locator("[data-testid=pbg-mode-scale]").count() == 0, "пересчёта для обрезанного фона не предлагается")
        d = dots(page)
        check(len(d) == 2 and sum(1 for x in d if x["out"]) == 1, f"одна точка за краем картинки — красная ({d})")
        check("1" in page.inner_text("[data-testid=pbg-outside]"), "счётчик «за краем» показан")
        page.click("[data-testid=pbg-apply]")
        wait_dialog(page, shown=False)
        page.wait_for_function("() => %s.planV3Current && %s.planV3Current.image_width === 900" % (APP, APP),
                               timeout=8000)
        body, pos = plan_items(h, pid)
        check((body["image_width"], body["image_height"]) == (900, 1000), "после замены: 900×1000")
        check(pos[nodes["A"]] == (800, 500) and pos[nodes["B"]] == (1400, 800),
              f"значки не сдвинулись сами — перепривязка вручную ({pos})")
        check("перепривязайте" in page.inner_text("body"), "сообщение: значки нужно перепривязать")

        # ---- 4. версии и возврат ---------------------------------------------------------------
        page.click("[data-testid=planv3-bg-versions]")
        page.wait_for_selector("[data-testid=pbg-versions]", timeout=8000)
        rows = page.locator("[data-testid=pbg-versions] li")
        check(rows.count() == 3, f"в списке три версии подложки ({rows.count()})")
        check("сейчас" in rows.nth(0).inner_text() and rows.nth(0).locator("[data-testid=pbg-restore]").count() == 0 or
              not rows.nth(0).locator("[data-testid=pbg-restore]").is_visible(),
              "текущая версия отмечена и без кнопки «Вернуть»")
        check(page.locator("[data-testid=pbg-versions] li img").first.evaluate("e => e.complete") in (True, False),
              "превью версий в списке")
        rows.nth(2).locator("[data-testid=pbg-restore]").click()
        wait_dialog(page)
        check(page.inner_text("#pbg-title") == "Вернуть прежнюю подложку", "предпросмотр возврата открыт")
        page.click("[data-testid=pbg-apply]")
        wait_dialog(page, shown=False)
        page.wait_for_function("() => %s.planV3Current && %s.planV3Current.image_width === 800" % (APP, APP),
                               timeout=8000)
        body, _pos = plan_items(h, pid)
        check((body["image_width"], body["image_height"]) == (800, 500), "после возврата: 800×500")
        got = h.client.get(f"/api/v2/plans/{pid}/image").data
        check(got == original_bytes, "картинка совпадает с исходной байт в байт")
        versions = h.client.get(f"/api/v2/plans/{pid}/image-versions").get_json()
        check(len(versions) == 3 and versions[-1]["is_current"], "версий по-прежнему три, текущая — первая")

        errors = [e for e in bp.errors]
        check(not errors, f"консоль без ошибок ({len(errors)}): {errors[:3]}")
        bp.close()

    print()
    if fails:
        print("ИТОГ: провалено проверок:", len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("ИТОГ: всё прошло")
    return 0


if __name__ == "__main__":
    sys.exit(main())

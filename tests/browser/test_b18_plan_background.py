"""Выпуск 0.22.4: подложка в «Плане v3».

Найдено Киром 19.10.2026 после обновления до 0.22.3: в «Плане v3» нет кнопки,
чтобы загрузить подложку (план помещения). Её и не было: «Создать план» делал
только пустой холст, а подложки раньше приходили из старых планов, которые
убрал чистый старт. Сервер загрузку умел всегда (`POST /api/v2/plans` с полем
`file`, `POST /api/v2/plans/<id>/image`), не хватало интерфейса.

Сценарий (настоящий Chromium, настоящее Flask-приложение):
  1. Пустая база: в форме «Создать план» есть поле «Подложка».
  2. План «Этаж 1» с картинкой 800×500 → вид `floor`, размеры картинки, на
     карте слой картинки с адресом `/api/v2/plans/<id>/image?v=…`, картинка
     загрузилась (naturalWidth = 800).
  3. «Щит» ставится кликом по карте, координаты — в пикселях картинки.
  4. «Заменить подложку…» → окно предпросмотра (партия 14, A30) → «Заменить подложку» →
     картинка 1000×600: размеры плана и картинки на
     карте новые (адрес другой — кэш браузера не мешает), значок щита на месте.
  5. «+ План» без файла → пустой холст `single_line`; у него кнопки «Заменить
     подложку…» нет.
  6. Не картинка → понятный отказ, план не создан.
  7. R16 (20.10.2026): план 7000×5000 (35 Мп — раньше отказ «слишком большая
     площадь») загружается целиком, без уменьшения; картинка 9000×1200 (больше
     8192 px по стороне) — браузер сам уменьшает до 8192×1092 и говорит об этом.
  8. Консоль без ошибок.

Запуск: python tests/browser/test_b18_plan_background.py
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def make_png(width: int, height: int, rgb=(214, 226, 240)) -> bytes:
    """Настоящий PNG (декодируется браузером), без сторонних библиотек.
    Сжатие построчно — большой план не держит в памяти все сырые байты."""
    row = b"\x00" + bytes(rgb) * width
    comp = zlib.compressobj(9)
    raw = b"".join(comp.compress(row) for _ in range(height)) + comp.flush()

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def api(page, path):
    return page.evaluate(f"fetch({path!r}).then(r=>r.json())")


def image_layer(page):
    return page.evaluate("""() => {
        const img = document.querySelector('.leaflet-container img.leaflet-image-layer');
        return img ? {src: img.getAttribute('src'), w: img.naturalWidth, h: img.naturalHeight,
                      done: img.complete} : null;
    }""")


def wait_image(page, width, tries=20):
    for _ in range(tries):
        layer = image_layer(page)
        if layer and layer["done"] and layer["w"] == width:
            return layer
        page.wait_for_timeout(250)
    return image_layer(page)


def main() -> int:
    tmp = tempfile.mkdtemp()
    small = os.path.join(tmp, "floor-800x500.png")
    big = os.path.join(tmp, "floor-1000x600.png")
    bad = os.path.join(tmp, "not-an-image.png")
    with open(small, "wb") as f:
        f.write(make_png(800, 500))
    with open(big, "wb") as f:
        f.write(make_png(1000, 600, rgb=(232, 236, 214)))
    huge_area = os.path.join(tmp, "floor-7000x5000.png")
    too_wide = os.path.join(tmp, "floor-9000x1200.png")
    with open(huge_area, "wb") as f:
        f.write(make_png(7000, 5000, rgb=(240, 240, 232)))
    with open(too_wide, "wb") as f:
        f.write(make_png(9000, 1200, rgb=(226, 240, 230)))
    with open(bad, "wb") as f:
        f.write("это не картинка".encode("utf-8"))

    with Harness() as h:
        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1000)
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(1000)

        # ---- 1. пустая база: поле подложки ------------------------------------
        check(page.locator("[data-testid=planv3-new-file]:visible").count() == 1,
              "в форме «Создать план» на пустой базе есть поле «Подложка»")

        # ---- 6 (сначала). не картинка — отказ, плана нет -----------------------
        page.fill("input[placeholder='Название плана']:visible", "Битый")
        page.set_input_files("[data-testid=planv3-new-file]:visible", bad)
        page.click("button:has-text('Создать план'):visible")
        page.wait_for_timeout(1200)
        check(api(page, "/api/v2/plans") == [], "не картинка: план не создан")
        body = page.inner_text("body")
        check("PNG" in body or "JPEG" in body or "изображ" in body.lower(),
              "не картинка: на экране понятный отказ про формат")

        # ---- 2. план с подложкой 800×500 ---------------------------------------
        page.fill("input[placeholder='Название плана']:visible", "Этаж 1")
        page.set_input_files("[data-testid=planv3-new-file]:visible", small)
        page.click("button:has-text('Создать план'):visible")
        page.wait_for_timeout(1800)
        plans = api(page, "/api/v2/plans")
        check(len(plans) == 1, f"создан один план ({[p['name'] for p in plans]})")
        p1 = plans[0] if plans else {}
        check(p1.get("plan_kind") == "floor" and p1.get("image_width") == 800
              and p1.get("image_height") == 500,
              f"план с подложкой: вид floor, 800×500 ({p1.get('plan_kind')}, "
              f"{p1.get('image_width')}×{p1.get('image_height')})")
        layer = wait_image(page, 800)
        check(bool(layer) and f"/api/v2/plans/{p1.get('id')}/image?v=" in (layer or {}).get("src", ""),
              f"на карте слой подложки с адресом плана ({layer and layer['src']})")
        check(bool(layer) and layer["w"] == 800 and layer["h"] == 500,
              f"картинка загрузилась в браузере ({layer and (layer['w'], layer['h'])})")
        check(page.locator("[data-testid=planv3-replace-bg]:visible").count() == 1,
              "у плана с подложкой есть «Заменить подложку…»")

        # ---- 3. щит кликом по подложке -----------------------------------------
        page.click("button:has-text('+ Элемент схемы')")
        page.wait_for_timeout(200)
        page.select_option("[data-testid=planv3-kind-select]", "panel")
        page.fill("input[placeholder='Название']", "Щит 1")
        box = page.locator(".leaflet-container:visible").first.bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
        page.wait_for_timeout(1500)
        plan_now = api(page, f"/api/v2/plans/{p1['id']}")
        items = [it for it in plan_now.get("items", []) if it["kind"] == "node"]
        check(len(items) == 1, f"щит поставлен на план ({len(items)})")
        g = items[0]["geometry"] if items else {}
        check(bool(g) and 0 <= g["x"] <= 800 and 0 <= g["y"] <= 500,
              f"координаты щита — в пикселях картинки 800×500 ({g})")
        check(bool(items) and items[0].get("coord_space") == "image_px_xy_v2",
              f"система координат значка — картинка ({items and items[0].get('coord_space')})")

        # ---- 4. заменить подложку ------------------------------------------------
        src_before = (layer or {}).get("src")
        page.set_input_files("[data-testid=planv3-replace-bg] input", big)
        # Партия 14 (A30): сначала предпросмотр — пока не нажато «Заменить», ничего не записано
        page.wait_for_selector("[data-testid=pbg-dialog]", timeout=8000)
        check(api(page, f"/api/v2/plans/{p1['id']}").get("image_width") == 800,
              "до подтверждения подложка не заменена")
        page.click("[data-testid=pbg-apply]")
        page.wait_for_function("() => !document.querySelector('[data-testid=pbg-dialog]')", timeout=8000)
        page.wait_for_timeout(1500)
        p1b = api(page, f"/api/v2/plans/{p1['id']}")
        check(p1b.get("image_width") == 1000 and p1b.get("image_height") == 600,
              f"подложка заменена: 1000×600 ({p1b.get('image_width')}×{p1b.get('image_height')})")
        layer2 = wait_image(page, 1000)
        check(bool(layer2) and layer2["src"] != src_before and layer2["w"] == 1000,
              f"на карте новая картинка, адрес другой ({src_before} → {layer2 and layer2['src']}, "
              f"ширина {layer2 and layer2['w']})")
        items_b = [it for it in p1b.get("items", []) if it["kind"] == "node"]
        check(bool(items_b) and items_b[0]["geometry"] == g,
              "значок щита после замены на прежних координатах")
        check("Подложка заменена" in page.inner_text("body"),
              "сообщение «Подложка заменена…» с напоминанием проверить значки")
        bp.screenshot("b18_floor_plan")

        # ---- 5. «+ План» без файла — пустой холст ------------------------------
        page.click("[data-testid=planv3-new-plan]")
        page.wait_for_timeout(300)
        form = page.locator("[data-testid=planv3-new-plan-form]:visible")
        check(form.count() == 1, "«+ План» открывает форму нового плана")
        form.locator("input[placeholder='Название плана']").fill("Схема")
        form.locator("button", has_text="Создать план").click()
        page.wait_for_timeout(1800)
        plans = api(page, "/api/v2/plans")
        p2 = next((p for p in plans if p["name"] == "Схема"), {})
        check(p2.get("plan_kind") == "single_line" and not p2.get("image_width"),
              f"план без файла — пустой холст ({p2.get('plan_kind')}, {p2.get('image_width')})")
        current = page.evaluate(APP + ".planV3CurrentId")
        check(current == p2.get("id"), "открыт только что созданный план")
        check(page.locator("[data-testid=planv3-replace-bg]:visible").count() == 0,
              "у пустого холста кнопки «Заменить подложку…» нет")
        check(page.locator("[data-testid=planv3-new-plan-form]:visible").count() == 0,
              "форма «+ План» закрылась после создания")
        check(image_layer(page) is None, "на пустом холсте слоя картинки нет")
        bp.screenshot("b18_plan_background")

        # ---- 7. R16: большие картинки ------------------------------------------
        def create_with(name, path, wait_ms=4000):
            page.click("[data-testid=planv3-new-plan]")
            page.wait_for_timeout(300)
            f = page.locator("[data-testid=planv3-new-plan-form]:visible")
            f.locator("input[placeholder='Название плана']").fill(name)
            page.set_input_files("[data-testid=planv3-new-plan-form] [data-testid=planv3-new-file]", path)
            f.locator("button", has_text="Создать план").click()
            for _ in range(int(wait_ms / 250)):
                page.wait_for_timeout(250)
                got = next((p for p in api(page, "/api/v2/plans") if p["name"] == name), None)
                if got:
                    return got
            return {}

        p35 = create_with("35 Мп", huge_area)
        check((p35.get("image_width"), p35.get("image_height")) == (7000, 5000),
              f"35 Мп (раньше — отказ по площади) загружен целиком: "
              f"{p35.get('image_width')}×{p35.get('image_height')}")
        check("уменьшил" not in page.inner_text("body"),
              "картинку в пределах 8192 px браузер не уменьшал")
        layer35 = wait_image(page, 7000, tries=40)
        check(bool(layer35) and layer35["w"] == 7000,
              f"большая подложка показана в браузере ({layer35 and layer35['w']})")

        pw = create_with("Широкий", too_wide)
        check((pw.get("image_width"), pw.get("image_height")) == (8192, 1092),
              f"9000×1200 браузер уменьшил до 8192×1092 и загрузил: "
              f"{pw.get('image_width')}×{pw.get('image_height')}")
        check("уменьшил её до 8192×1092" in page.inner_text("body"),
              "сообщение: картинка была больше 8192 px — браузер уменьшил её до 8192×1092")

        # Ровно один ожидаемый отказ сервера — шаг 6 («не картинка» → 400); как
        # 409 в b07/b08, браузер пишет его в консоль сам. Других ошибок быть не должно.
        expected_400 = [e for e in bp.errors if "status of 400" in e]
        # Шаг 7 не должен давать ни одного отказа сервера.
        check(len(expected_400) <= 1, f"400 только от шага «не картинка» ({len(expected_400)})")
        errs = [e for e in bp.errors if "status of 400" not in e]
        check(not errs, f"консоль без других ошибок ({len(errs)}): " + "; ".join(errs[:5]))
        bp.close()

    if fails:
        print(f"\n{len(fails)} проверок не прошли")
        return 1
    print("\nВсе проверки подложки «Плана v3» прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Партия 13, 13.6 (§8.5, H13-C): контраст пар токенов обеих тем — скрипт проверки и таблица.

Токены темы лежат в index.html (`:root[data-theme="dark"]` и `:root[data-theme="light"]`).
Скрипт считает контраст WCAG 2.x (относительная яркость sRGB) для пар «цвет → поверхность»:

  текст             ≥ 4,5 : 1  — txt, txt2, txt3, акцентные и статусные цвета на bg / bg2 / bg3;
                              белый текст на заливках accent и err-fill (кнопки «основная»/«опасная»,
                              всплывающая ошибка);
  границы и значки  ≥ 3   : 1  — --line-ctl (граница поля и кнопки), цвета статусов и акцента
                              как значки/маркеры, кольцо фокуса, на bg / bg2 / bg3.

`--line` (декоративные разделители карточек и строк) в проверку не входит: WCAG 1.4.11 требует
3:1 только у границ, без которых элемент нельзя найти. Для полей и кнопок разметка подменяет
--line на --line-ctl (проверяется ниже). Нарушения исправляются в токенах index.html, а не
точечно в экранах.

Самостоятельный скрипт (не pytest). Печатает таблицу пар; код возврата 1 при нарушении:
    python tests/test_contrast_tokens.py
"""

from __future__ import annotations

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(REPO_ROOT, "wb_energy_meter", "static", "index.html")

TEXT_MIN = 4.5
NONTEXT_MIN = 3.0
SURFACES = ("bg", "bg2", "bg3")

TEXT_TOKENS = ("txt", "txt2", "txt3", "accent2", "ok", "warn", "err", "info")
NONTEXT_TOKENS = ("line-ctl", "accent", "accent2", "ok", "warn", "err", "info", "idle")
WHITE = "#ffffff"
WHITE_FILLS = ("accent", "err-fill")


def _tokens(html: str, theme: str) -> dict:
    m = re.search(r':root\[data-theme="%s"\]\{(.*?)\n\}' % theme, html, re.S)
    if not m:
        raise SystemExit("в index.html нет блока токенов темы %r" % theme)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", m.group(1)))


def _lum(color: str) -> float:
    h = color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    if la < lb:
        la, lb = lb, la
    return (la + 0.05) / (lb + 0.05)


def main() -> int:
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    bad: list[str] = []
    rows: list[tuple] = []
    for theme in ("dark", "light"):
        t = _tokens(html, theme)
        for tok in TEXT_TOKENS:
            for s in SURFACES:
                r = ratio(t[tok], t[s])
                rows.append((theme, "текст", "--%s на --%s" % (tok, s), r, TEXT_MIN))
        for fill in WHITE_FILLS:
            r = ratio(WHITE, t[fill])
            rows.append((theme, "текст", "белый на заливке --%s" % fill, r, TEXT_MIN))
        for tok in NONTEXT_TOKENS:
            for s in SURFACES:
                r = ratio(t[tok], t[s])
                rows.append((theme, "граница/значок", "--%s на --%s" % (tok, s), r, NONTEXT_MIN))

    print("| тема | вид | пара | контраст | порог | итог |")
    print("|---|---|---|---|---|---|")
    for theme, kind, pair, r, need in rows:
        ok = r >= need
        print("| %s | %s | %s | %.2f | %.1f | %s |" % (theme, kind, pair, r, need, "ok" if ok else "НАРУШЕНИЕ"))
        if not ok:
            bad.append("%s: %s = %.2f (< %.1f)" % (theme, pair, r, need))

    # управляющие элементы действительно берут --line-ctl (иначе токен ничего не даёт)
    rule = re.search(r"button,input,select,textarea,\.btn,\.btn-sm,\.ico-btn\{--line:var\(--line-ctl\)\}", html)
    print("\n%s правило «поля и кнопки берут --line-ctl» есть в index.html" % ("OK  " if rule else "FAIL"))
    if not rule:
        bad.append("в index.html нет правила --line: var(--line-ctl) для полей и кнопок")

    print("\nпар проверено: %d, нарушений: %d" % (len(rows), len(bad)))
    for b in bad:
        print("  -", b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

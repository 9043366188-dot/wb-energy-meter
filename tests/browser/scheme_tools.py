"""Общие действия браузерных сценариев над однолинейной схемой «Плана v3» (не сценарий: run.py его не запускает).

`line_hit_point` — экранная точка на самой линии схемы, в которую попадает настоящий клик мышью
(проверено `document.elementFromPoint`). Нужна test_b31 (A32) и test_b32 (A34): кликать по линии
схемы надо так, как это делает человек, а не вызовом `openInspectorInPlace`.
"""
from __future__ import annotations

APP = "document.querySelector('[x-data]')._x_dataStack[0]"


def line_hit_point(page, edge_id: int):
    """Точка на линии схемы (по id связи), в которую реально попадает клик, либо None.

    Идём по отрезкам трассы с конца (у значка нагрузки линии не общие, у щита — общая «шина») и
    берём первую точку, под которой лежит штрих именно этой линии, а не чужой линии или значок.
    Карту нужно заранее показать на экране (scroll_into_view_if_needed): вне окна браузера
    elementFromPoint возвращает null."""
    return page.evaluate("""(eid) => {
      const a = %s;
      const v = a.planV3Current.edges.find(e => e.edge_id === eid);
      const layer = v && _planV3EdgeLayers.get(String(v.id));
      if (!layer) return null;
      const c = _planV3Map.getContainer(), r = c.getBoundingClientRect();
      const ox = r.left + c.clientLeft, oy = r.top + c.clientTop;
      const pts = layer.getLatLngs().map(ll => _planV3Map.latLngToContainerPoint(ll));
      const el = layer.getElement();
      for (let i = pts.length - 1; i > 0; i--) {
        const A = pts[i - 1], B = pts[i];
        for (let t = 0.5; t <= 0.9; t += 0.05) {
          for (const tt of [t, 1 - t]) {
            const x = ox + A.x + (B.x - A.x) * tt, y = oy + A.y + (B.y - A.y) * tt;
            const hit = document.elementFromPoint(x, y);
            if (hit && el && (hit === el || el.contains(hit))) return {x, y};
          }
        }
      }
      return null;
    }""" % APP, edge_id)

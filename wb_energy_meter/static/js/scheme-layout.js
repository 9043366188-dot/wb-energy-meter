// scheme-layout.js — чистые функции раскладки и трасс однолинейной схемы
// (партия 14, 14.2/14.3, docs/design-single-line.md). БЕЗ DOM, Leaflet и Alpine:
// файл подключается в index.html обычным <script> (window.SchemeLayout) и
// проверяется под `node` (tests/test_step69_single_line_layout.py).
//
// Координаты — холст схемы (canvas_xy_v2): X вправо, Y ВНИЗ, начало в левом
// верхнем углу. Положение узла — центр значка. Подпись узла стоит справа от
// значка, на его высоте, поэтому линия, идущая вниз от центра значка, подпись
// не пересекает.
//
// Что делает:
//   layoutTree      — послойная раскладка радиального дерева (вводы сверху, щиты
//                     по глубине, нагрузки нижним рядом); режимы 'all' и 'unplaced';
//   routeEdges      — ортогональные трассы: автоматические (общая «шина» у
//                     отходящих линий одного узла) и ручные (по сохранённым точкам);
//   snapOrthogonal  — привязка нарисованной ломаной к горизонтали/вертикали;
//   isOrthogonal, nodeBox, findOverlaps, labelAnchor — вспомогательные.
// Функции детерминированы: тот же вход — те же числа.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.SchemeLayout = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  const M = {
    iconW: 36, iconH: 36,        // значок узла
    labelGap: 6, labelPad: 8,    // зазор значок→подпись, поля подписи
    labelCharW: 7.5,             // оценка ширины символа 12px-шрифта
    labelMaxW: 220,              // подпись длиннее обрезается многоточием (CSS)
    minSlotW: 128,               // не уже: подпись линии (≤130 px) не налезает на соседа
    gapX: 40, gapXMin: 12,       // зазор между соседними поддеревьями
    rowH: 150, rowHMin: 96,      // расстояние между рядами
    marginX: 60, marginTop: 60, marginBottom: 60,
    minDrop: 56,                 // ближе — потомок «не ниже» родителя, шина не нужна
    busOffset: 70,               // шина ниже родителя, но не ниже середины до потомка
    tol: 0.5,                    // допуск ортогональности, единиц холста
  };

  // ---------- вспомогательное ----------

  // Естественное сравнение: «Щит 2» < «Щит 10»; без localeCompare — результат
  // не зависит от локали окружения.
  function naturalCompare(a, b) {
    const sa = String(a == null ? '' : a).toLowerCase();
    const sb = String(b == null ? '' : b).toLowerCase();
    const ta = sa.match(/\d+|\D+/g) || [];
    const tb = sb.match(/\d+|\D+/g) || [];
    const n = Math.min(ta.length, tb.length);
    for (let i = 0; i < n; i++) {
      const x = ta[i], y = tb[i];
      const dx = /^\d/.test(x), dy = /^\d/.test(y);
      if (dx && dy) {
        const d = Number(x) - Number(y);
        if (d !== 0) return d < 0 ? -1 : 1;
      } else if (x !== y) {
        return x < y ? -1 : 1;
      }
    }
    return ta.length === tb.length ? 0 : (ta.length < tb.length ? -1 : 1);
  }

  const KIND_RANK = { source: 0, panel: 1, junction: 2, load: 3 };
  function kindRank(k) { return k in KIND_RANK ? KIND_RANK[k] : 4; }

  function labelWidth(text) {
    const n = String(text == null ? '' : text).length;
    return Math.min(M.labelMaxW, Math.round(n * M.labelCharW) + M.labelPad);
  }
  function nodeWidth(label) {
    return Math.max(M.minSlotW, M.iconW + M.labelGap + labelWidth(label));
  }

  // Рамка значка с подписью. (x, y) — центр значка.
  function nodeBox(pos, label) {
    const w = M.iconW + M.labelGap + labelWidth(label);
    return {
      left: pos.x - M.iconW / 2, right: pos.x - M.iconW / 2 + w,
      top: pos.y - M.iconH / 2, bottom: pos.y + M.iconH / 2,
    };
  }
  function boxesOverlap(a, b, margin) {
    const m = margin || 0;
    return a.left < b.right + m && b.left < a.right + m &&
           a.top < b.bottom + m && b.top < a.bottom + m;
  }
  // boxes: {id: box} → [[idA, idB], ...] (id как строки, порядок возрастания)
  function findOverlaps(boxes) {
    const ids = Object.keys(boxes).sort((p, q) => Number(p) - Number(q));
    const out = [];
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        if (boxesOverlap(boxes[ids[i]], boxes[ids[j]], 0)) out.push([ids[i], ids[j]]);
      }
    }
    return out;
  }

  function isOrthogonal(points, tol) {
    const t = tol == null ? M.tol : tol;
    for (let i = 1; i < points.length; i++) {
      const dx = Math.abs(points[i][0] - points[i - 1][0]);
      const dy = Math.abs(points[i][1] - points[i - 1][1]);
      if (dx > t && dy > t) return false;
    }
    return true;
  }

  // Убирает повторы и лишние точки на прямой (для осевых ломаных).
  function simplify(points) {
    const out = [];
    for (const p of points) {
      const last = out[out.length - 1];
      if (last && Math.abs(last[0] - p[0]) < 1e-9 && Math.abs(last[1] - p[1]) < 1e-9) continue;
      out.push([p[0], p[1]]);
    }
    for (let i = out.length - 2; i >= 1; i--) {
      const a = out[i - 1], b = out[i], c = out[i + 1];
      const sameX = Math.abs(a[0] - b[0]) < 1e-9 && Math.abs(b[0] - c[0]) < 1e-9;
      const sameY = Math.abs(a[1] - b[1]) < 1e-9 && Math.abs(b[1] - c[1]) < 1e-9;
      if (sameX || sameY) out.splice(i, 1);
    }
    return out;
  }

  // ---------- дерево ----------

  // nodes: [{id, kind, label}], edges: [{id, from, to}] → {roots, children, parent, order}
  function buildTree(nodes, edges) {
    const byId = new Map();
    nodes.forEach(n => byId.set(n.id, n));
    const sortedEdges = edges.slice().sort((a, b) => a.id - b.id);
    const parent = new Map();
    sortedEdges.forEach(e => {
      if (e.from === e.to || !byId.has(e.from) || !byId.has(e.to)) return;
      if (!parent.has(e.to)) parent.set(e.to, e.from);   // первый родитель — по id связи
    });
    const cmp = (a, b) => {
      const A = byId.get(a), B = byId.get(b);
      return kindRank(A.kind) - kindRank(B.kind)
          || naturalCompare(A.label, B.label) || (a - b);
    };
    let children = new Map();
    const rebuild = () => {
      children = new Map();
      byId.forEach((_, id) => children.set(id, []));
      parent.forEach((p, c) => children.get(p).push(c));
      children.forEach(list => list.sort(cmp));
    };
    rebuild();
    const rootsOf = () => nodes.map(n => n.id).filter(id => !parent.has(id)).sort(cmp);
    // Циклы: узлы, недостижимые от корней, — разрываем по наименьшему id.
    for (;;) {
      const seen = new Set();
      const walk = (id) => {
        const stack = [id];
        while (stack.length) {
          const x = stack.pop();
          if (seen.has(x)) continue;
          seen.add(x);
          children.get(x).forEach(c => stack.push(c));
        }
      };
      rootsOf().forEach(walk);
      const lost = nodes.map(n => n.id).filter(id => !seen.has(id)).sort((a, b) => a - b);
      if (!lost.length) break;
      parent.delete(lost[0]);
      rebuild();
    }
    const roots = rootsOf();
    const order = [];       // родители раньше потомков, порядок детерминирован
    const visit = (id) => { order.push(id); children.get(id).forEach(visit); };
    roots.forEach(visit);
    return { roots, children, parent, order, byId, cmp };
  }

  // ---------- раскладка ----------

  function layoutAll(tree, canvas, gapX, rowH) {
    const { roots, children, byId } = tree;
    const isBottomLoad = (id) => byId.get(id).kind === 'load' && children.get(id).length === 0;

    // глубина → ряд; нагрузки-листья — нижний ряд
    const depth = new Map();
    const setDepth = (id, d) => { depth.set(id, d); children.get(id).forEach(c => setDepth(c, d + 1)); };
    roots.forEach(id => setDepth(id, byId.get(id).kind === 'source' ? 0 : 1));
    const usedDepths = Array.from(new Set(
      tree.order.filter(id => !isBottomLoad(id)).map(id => depth.get(id)))).sort((a, b) => a - b);
    const rowOfDepth = new Map(usedDepths.map((d, i) => [d, i]));
    const bottomRow = usedDepths.length;
    const rowOf = (id) => isBottomLoad(id) ? bottomRow : rowOfDepth.get(depth.get(id));
    const hasBottom = tree.order.some(isBottomLoad);
    const rows = usedDepths.length + (hasBottom ? 1 : 0);

    // ширины интервалов (снизу вверх)
    const sw = new Map();
    const calcW = (id) => {
      const own = nodeWidth(byId.get(id).label);
      const kids = children.get(id);
      let w = own;
      if (kids.length) {
        let sum = gapX * (kids.length - 1);
        kids.forEach(c => { sum += calcW(c); });
        w = Math.max(own, sum);
      }
      sw.set(id, w);
      return w;
    };
    roots.forEach(calcW);
    let total = gapX * Math.max(0, roots.length - 1);
    roots.forEach(id => { total += sw.get(id); });

    const fitsW = total + 2 * M.marginX <= canvas.width;
    const startX = fitsW ? M.marginX + (canvas.width - 2 * M.marginX - total) / 2 : M.marginX;

    const xs = new Map();   // центр значка
    const place = (id, left) => {
      const own = nodeWidth(byId.get(id).label);
      const kids = children.get(id);
      const width = sw.get(id);
      let x;
      if (!kids.length) {
        x = left + M.iconW / 2 + (width - own) / 2;
      } else {
        let sum = gapX * (kids.length - 1);
        kids.forEach(c => { sum += sw.get(c); });
        let cur = left + (width - sum) / 2;
        kids.forEach(c => { place(c, cur); cur += sw.get(c) + gapX; });
        x = (xs.get(kids[0]) + xs.get(kids[kids.length - 1])) / 2;
        x = Math.min(Math.max(x, left + M.iconW / 2), left + width - own + M.iconW / 2);
      }
      xs.set(id, x);
    };
    let cursor = startX;
    roots.forEach(id => { place(id, cursor); cursor += sw.get(id) + gapX; });

    const positions = {};
    tree.order.forEach(id => {
      positions[id] = {
        x: Math.round(xs.get(id)),
        y: Math.round(M.marginTop + M.iconH / 2 + rowOf(id) * rowH),
      };
    });
    const neededW = Math.ceil(total + 2 * M.marginX);
    const neededH = Math.ceil(M.marginTop + M.iconH + Math.max(0, rows - 1) * rowH + M.marginBottom);
    return { positions, rows, needed: { width: neededW, height: neededH } };
  }

  // Режим 'unplaced': стоящие узлы не двигаются, новые встают под своего
  // родителя (или по общей раскладке — если родителя нет на схеме), сдвигаясь
  // в ближайшее свободное место.
  function layoutUnplaced(tree, canvas, fixed, full) {
    const { children, parent, byId, order } = tree;
    const placed = {};
    Object.keys(fixed).forEach(k => { placed[k] = { x: fixed[k].x, y: fixed[k].y }; });
    const boxes = [];
    Object.keys(placed).forEach(k => {
      const id = Number(k);
      if (byId.has(id)) boxes.push(nodeBox(placed[k], byId.get(id).label));
    });
    const out = {};
    let overflow = false;
    const rowH = full.rowH;
    const step = 20;
    const maxX = canvas.width, maxY = canvas.height;

    const free = (id, x, y) => {
      const b = nodeBox({ x, y }, byId.get(id).label);
      if (b.left < 0 || b.right > maxX || b.top < 0 || b.bottom > maxY) return false;
      return !boxes.some(o => boxesOverlap(b, o, M.gapXMin / 2));
    };
    const settle = (id, dx, dy) => {
      const rowsAvail = Math.max(1, Math.floor((maxY - dy) / rowH) + 1);
      for (let r = 0; r < rowsAvail; r++) {
        const y = dy + r * rowH;
        for (let k = 0; k * step <= maxX; k++) {
          for (const sgn of (k === 0 ? [1] : [1, -1])) {
            const x = dx + sgn * k * step;
            if (free(id, x, y)) return { x: Math.round(x), y: Math.round(y) };
          }
        }
      }
      return null;
    };

    order.forEach(id => {
      if (placed[id] != null) return;
      const p = parent.get(id);
      let want;
      if (p != null && placed[p] != null) {
        const sibs = children.get(p).filter(c => placed[c] == null);
        const widths = sibs.map(c => nodeWidth(byId.get(c).label));
        const total = widths.reduce((a, b) => a + b, 0) + M.gapX * (sibs.length - 1);
        let offset = 0;
        for (let i = 0; i < sibs.indexOf(id); i++) offset += widths[i] + M.gapX;
        want = { x: placed[p].x - total / 2 + offset + M.iconW / 2, y: placed[p].y + rowH };
      } else {
        want = full.positions[id];
      }
      const spot = settle(id, want.x, want.y);
      let pos = spot;
      if (!spot) {
        overflow = true;
        pos = { x: Math.round(want.x), y: Math.round(want.y) };
      }
      placed[id] = pos;
      out[id] = pos;
      boxes.push(nodeBox(pos, byId.get(id).label));
    });
    return { positions: out, overflow };
  }

  /**
   * input: {
   *   nodes: [{id, kind, label}],   // label — текст подписи (для оценки ширины)
   *   edges: [{id, from, to}],      // from — питающий узел, to — получатель
   *   canvas: {width, height},
   *   mode: 'all' | 'unplaced',
   *   fixed: {nodeId: {x, y}}       // для 'unplaced': уже стоящие узлы
   * }
   * → { positions: {nodeId: {x, y}}, fits, needed: {width, height}, rows }
   * 'all' возвращает позиции всех узлов; 'unplaced' — только узлов, которых нет в fixed.
   */
  function layoutTree(input) {
    const canvas = input.canvas;
    const tree = buildTree(input.nodes, input.edges);
    // Сначала обычный зазор; не влезло по ширине — минимальный.
    let rowH = M.rowH;
    let full = layoutAll(tree, canvas, M.gapX, rowH);
    if (full.needed.width > canvas.width) {
      full = layoutAll(tree, canvas, M.gapXMin, rowH);
    }
    if (full.needed.height > canvas.height && full.rows > 1) {
      const avail = canvas.height - M.marginTop - M.marginBottom - M.iconH;
      rowH = Math.max(M.rowHMin, Math.floor(avail / (full.rows - 1)));
      full = layoutAll(tree, canvas,
        full.needed.width > canvas.width ? M.gapXMin : M.gapX, rowH);
    }
    const fits = full.needed.width <= canvas.width && full.needed.height <= canvas.height;
    full.rowH = rowH;
    const fixedIds = Object.keys(input.fixed || {}).filter(k => tree.byId.has(Number(k)));
    if (input.mode === 'unplaced' && fixedIds.length) {
      const r = layoutUnplaced(tree, canvas, input.fixed, full);
      return { positions: r.positions, fits: fits && !r.overflow,
               needed: full.needed, rows: full.rows };
    }
    const positions = full.positions;
    if (input.mode === 'unplaced') {
      // ничего не стоит — то же, что полная раскладка, но возвращаем всё
      return { positions, fits, needed: full.needed, rows: full.rows };
    }
    return { positions, fits, needed: full.needed, rows: full.rows };
  }

  // ---------- трассы ----------

  /**
   * items: {itemId: {x, y}}   — центры значков
   * edges: [{id, from, to, waypoints}] — from/to — id значков; waypoints — ручная
   *        ломаная [[x,y],...] (вся, как в «Плане v2») или null/пусто — автоматическая.
   * → { routes: {edgeId: {points, manual, orthogonal}} }
   * У ручной трассы первая и последняя точки заменяются актуальными центрами
   * значков: сдвинули узел — концы тянутся за ним, а неортогональные отрезки
   * помечаются (orthogonal:false).
   */
  function routeEdges(input) {
    const items = input.items;
    const routes = {};
    const auto = new Map();   // from → [edge]
    input.edges.forEach(e => {
      const a = items[e.from], b = items[e.to];
      if (!a || !b) return;
      if (Array.isArray(e.waypoints) && e.waypoints.length >= 2) {
        const mid = e.waypoints.slice(1, -1).map(p => [p[0], p[1]]);
        const pts = simplify([[a.x, a.y]].concat(mid, [[b.x, b.y]]));
        routes[e.id] = { points: pts, manual: true, orthogonal: isOrthogonal(pts) };
      } else {
        if (!auto.has(e.from)) auto.set(e.from, []);
        auto.get(e.from).push(e);
      }
    });
    auto.forEach((list, from) => {
      const a = items[from];
      const below = list.filter(e => items[e.to].y > a.y + M.minDrop);
      let busY = null;
      if (below.length) {
        const nearest = Math.min.apply(null, below.map(e => items[e.to].y));
        busY = a.y + Math.min(M.busOffset, (nearest - a.y) / 2);
      }
      list.forEach(e => {
        const b = items[e.to];
        let pts;
        if (busY != null && b.y > a.y + M.minDrop) {
          pts = [[a.x, a.y], [a.x, busY], [b.x, busY], [b.x, b.y]];
        } else if (Math.abs(b.y - a.y) <= M.tol) {
          pts = [[a.x, a.y], [b.x, b.y]];
        } else {
          pts = [[a.x, a.y], [a.x, b.y], [b.x, b.y]];
        }
        pts = simplify(pts);
        routes[e.id] = { points: pts, manual: false, orthogonal: true };
      });
    });
    return { routes };
  }

  /**
   * Нарисованная пользователем ломаная (центры кликов) → полный путь от значка
   * from до значка to, где каждый отрезок горизонтален или вертикален.
   * Каждый следующий клик «прилипает» к оси, по которой он дальше от предыдущей
   * точки; в конце путь доводится до to одним изгибом.
   * → [[x,y], ...] включая концы (то, что хранится в waypoints).
   */
  function snapOrthogonal(drawn, from, to) {
    const pts = [[from.x, from.y]];
    let cur = [from.x, from.y];
    drawn.forEach(q => {
      const dx = Math.abs(q[0] - cur[0]), dy = Math.abs(q[1] - cur[1]);
      const nxt = dx >= dy ? [q[0], cur[1]] : [cur[0], q[1]];
      pts.push(nxt);
      cur = nxt;
    });
    if (Math.abs(cur[0] - to.x) > M.tol && Math.abs(cur[1] - to.y) > M.tol) {
      pts.push([to.x, cur[1]]);
    }
    pts.push([to.x, to.y]);
    return simplify(pts);
  }

  // Точка для подписи линии. Последний вертикальный отрезок (у получателя) длиннее 40 —
  // подпись у его верхнего конца (сразу под «шиной»: ниже стоит подпись самого значка);
  // иначе — середина самого длинного отрезка.
  function labelAnchor(points) {
    if (points.length < 2) return points[0] || [0, 0];
    const segs = [];
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1], b = points[i];
      segs.push({ a, b, len: Math.hypot(b[0] - a[0], b[1] - a[1]),
                  vertical: Math.abs(a[0] - b[0]) <= M.tol });
    }
    const last = segs[segs.length - 1];
    if (last.vertical && last.len >= 40) {
      return [last.a[0], Math.min(last.a[1], last.b[1]) + 12];
    }
    const s = segs.reduce((m, x) => (x.len > m.len ? x : m), segs[0]);
    return [(s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2];
  }

  return {
    METRICS: M, naturalCompare, labelWidth, nodeWidth, nodeBox, boxesOverlap, findOverlaps,
    isOrthogonal, simplify, buildTree, layoutTree, routeEdges, snapOrthogonal, labelAnchor,
  };
});

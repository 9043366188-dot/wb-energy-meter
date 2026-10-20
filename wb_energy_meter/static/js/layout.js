// layout.js — каркас адаптивности (партия 13, 13.1; ТЗ §8.5, docs/TZ-finish-plan.md).
//
// Брейкпоинты (синхронны с @media в index.html, блок «адаптивность»):
//   ≥ 1280   — дерево/холст и карточка рядом, как раньше;
//   768–1279 — карточка (инспектор) — выдвижная панель справа (.drawer);
//   360–767  — одна рабочая область, карточка — нижняя выдвижная панель.
//
// Этот файл не рисует раскладку — её делает CSS. Здесь только то, что без JS
// не сделать: какой брейкпоинт сейчас (для фокуса), viewport классического
// набора, высота шапки для панели, открытие/закрытие панели с возвратом фокуса
// и Escape. Состояние карточки одно и то же на всех ширинах — structInspector
// (structure.js); панель только показывает его по-другому.
(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  const MQ_NARROW = '(max-width:1279px)';
  const MQ_PHONE = '(max-width:767px)';
  // viewport: классический набор не переделывался (ТЗ 13.1: «не должен стать
  // хуже») — на телефоне он остаётся масштабированной страницей 1100 px, как
  // раньше; новый набор — адаптивный. В index.html стоит device-width (то, что
  // видят до старта скриптов и тесты); для классики JS возвращает прежнее.
  const VIEWPORT_V2 = 'width=device-width, initial-scale=1';
  const VIEWPORT_CLASSIC = 'width=1100';

  window.WBEM.parts.push(function layoutPart() {
    return {
      // Текущий брейкпоинт. Обновляется слушателями matchMedia в initLayout().
      layoutNarrow: false,
      layoutPhone: false,

      // «План v3»: ручное открытие панели обзора (список элементов, линий,
      // точек) на узких экранах, когда карточка не выбрана.
      planV3PanelOpen: false,

      _layoutOpener: null,

      initLayout() {
        try {
          const nq = window.matchMedia(MQ_NARROW);
          const pq = window.matchMedia(MQ_PHONE);
          this.layoutNarrow = nq.matches;
          this.layoutPhone = pq.matches;
          const onChange = () => {
            this.layoutNarrow = nq.matches;
            this.layoutPhone = pq.matches;
            this.layoutMeasureHeader();
          };
          if (nq.addEventListener) { nq.addEventListener('change', onChange); pq.addEventListener('change', onChange); }
          else if (nq.addListener) { nq.addListener(onChange); pq.addListener(onChange); }
        } catch (e) { /* без matchMedia остаётся «широкий» режим */ }

        this.layoutApplyViewport();
        this.$watch('uiSet', () => this.layoutApplyViewport());

        // Высота шапки нужна выдвижной панели (top: var(--hdr-h)): на узких
        // экранах шапка переносится на 2–3 строки.
        this.layoutMeasureHeader();
        try {
          const hdr = document.querySelector('header');
          if (hdr && window.ResizeObserver) new ResizeObserver(() => this.layoutMeasureHeader()).observe(hdr);
        } catch (e) { /* не критично */ }

        // Строки списков «Структуры» и панели «Плана v3» (data-kb-row, tabindex=0): Enter/Пробел
        // открывают карточку, стрелки/Home/End двигают фокус по строкам своего списка.
        document.addEventListener('keydown', (ev) => this.layoutRowKey(ev));

        // Открытие/закрытие панели: фокус внутрь и обратно, карта подстраивается.
        this.$watch(() => this.layoutDrawerShown(), (open, was) => this._layoutOnDrawerChange(!!open, !!was));
        this.$watch('tab', () => { this.planV3PanelOpen = false; });
      },

      layoutApplyViewport() {
        try {
          const m = document.querySelector('meta[name=viewport]');
          if (m) m.setAttribute('content', this.uiSet === 'classic' ? VIEWPORT_CLASSIC : VIEWPORT_V2);
        } catch (e) { /* не критично */ }
      },

      layoutMeasureHeader() {
        try {
          const hdr = document.querySelector('header');
          if (!hdr) return;
          const h = Math.round(hdr.getBoundingClientRect().height);
          // Шапка на телефоне не липкая — панель тогда прижата к низу, но
          // переменная всё равно честная.
          document.documentElement.style.setProperty('--hdr-h', h + 'px');
        } catch (e) { /* не критично */ }
      },

      // Какая панель на текущей вкладке: «Структура» — правая колонка с
      // карточкой, «План v3» — панель справа от карты. Другие вкладки панели
      // не имеют.
      layoutDrawerKind() {
        if (this.tab === 'structurev2') return 'struct';
        if (this.tab === 'planv3') return 'plan';
        return null;
      },

      // Панель «показана»: выбрана карточка (или она грузится/дала ошибку), а в
      // «Плане v3» — ещё и открыта вручную кнопкой «Панель».
      layoutDrawerShown() {
        const kind = this.layoutDrawerKind();
        if (!kind) return false;
        const card = !!(this.structInspector || this.structInspectorLoading || this.structInspectorError);
        return kind === 'plan' ? (card || this.planV3PanelOpen) : card;
      },

      layoutDrawerEl() {
        const kind = this.layoutDrawerKind();
        return kind ? document.querySelector('[data-drawer="' + kind + '"]') : null;
      },

      layoutCloseDrawer() {
        this.planV3PanelOpen = false;
        if (typeof this.closeInspector === 'function') this.closeInspector();
      },

      // Куда вернуть фокус, если элемент, открывший карточку, недоступен
      // (клик по карте, строка без фокуса): отмеченный data-focus-home элемент
      // текущей вкладки, иначе её кнопка в навигации.
      _layoutFocusHome() {
        const vis = Array.from(document.querySelectorAll('[data-focus-home]'))
          .find(e => e.offsetParent !== null);
        return vis || document.querySelector('nav button.active');
      },

      _layoutOnDrawerChange(open, was) {
        if (open && !was) {
          const a = document.activeElement;
          this._layoutOpener = (a && a !== document.body && a.tagName !== 'HTML') ? a : null;
          // На узком экране панель закрывает часть страницы — переносим фокус в
          // неё, иначе клавиатурный пользователь остаётся «под» панелью.
          if (this.layoutNarrow) {
            this.$nextTick(() => {
              const el = this.layoutDrawerEl();
              if (el) { try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); } }
            });
          }
        } else if (!open && was) {
          const back = this._layoutOpener;
          this._layoutOpener = null;
          // Возвращаем фокус, только если он сейчас в закрывающейся панели или
          // потерян (body): пользователь мог уйти в другое место сам.
          const a = document.activeElement;
          const lost = !a || a === document.body || a.tagName === 'HTML' ||
                       !!(a.closest && a.closest('[data-drawer]'));
          this.$nextTick(() => {
            if (lost) {
              const target = (back && document.contains(back) && back.offsetParent !== null) ? back : this._layoutFocusHome();
              if (target) { try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); } }
            }
          });
        }
        // Карта подстраивается под новую ширину (на ≥1280 колонка в сетке, на
        // узких панель поверх): зум и центр не трогаем.
        if (typeof this.planV3InvalidateMap === 'function') this.planV3InvalidateMap();
      },

      // Клавиатура в списках: строка — элемент с data-kb-row, список — ближайший data-kb-list.
      layoutRowKey(ev) {
        const el = ev.target;
        if (!el || !el.matches || !el.matches('[data-kb-row]')) return;
        if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
        const k = ev.key;
        if (k === 'Enter' || k === ' ' || k === 'Spacebar') { ev.preventDefault(); el.click(); return; }
        if (k !== 'ArrowDown' && k !== 'ArrowUp' && k !== 'Home' && k !== 'End') return;
        const list = el.closest('[data-kb-list]') || document;
        const rows = Array.from(list.querySelectorAll('[data-kb-row]')).filter(r => r.offsetParent !== null);
        const i = rows.indexOf(el);
        if (i < 0) return;
        let j = k === 'ArrowDown' ? i + 1 : k === 'ArrowUp' ? i - 1 : k === 'Home' ? 0 : rows.length - 1;
        j = Math.max(0, Math.min(rows.length - 1, j));
        ev.preventDefault();
        rows[j].focus();
      },

      // Escape (общий обработчик в core.js вызывает это, когда ни один
      // инструмент карты не отменялся): закрывает карточку/панель с возвратом
      // фокуса. Из текстового поля внутри панели первое нажатие только уводит
      // фокус на панель — черновик не теряется.
      layoutEscape(ev) {
        if (!this.layoutDrawerShown()) return;
        const t = ev && ev.target;
        const isField = !!(t && t.tagName && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) &&
                           !/^(checkbox|radio|button|submit)$/.test(t.type || ''));
        const el = this.layoutDrawerEl();
        if (isField) {
          if (el && el.contains(t)) { try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); } }
          return;
        }
        this.layoutCloseDrawer();
      },
    };
  });
})();

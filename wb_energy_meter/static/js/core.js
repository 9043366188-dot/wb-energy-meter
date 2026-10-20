// core.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось. Последний из
// выделенных файлов: общие хелперы уровня приложения, использующиеся
// в 2+ экранах (DEFAULT_UI_SET/UI_SET_TABS, _planCssVar,
// _planEscapeHtml, lsGet, fmtTime, fmtHour), плюс то, что осталось в
// самом app() (жизненный цикл, тема, статус, общие форматтеры).
// ---- общие модульные переменные/хелперы (несколько экранов) ----
const DEFAULT_UI_SET = 'classic';
// Партия 13, П3 (решение R5): вкладка «План v2» в наборе v2 скрыта — карта
// узлов сети теперь «План v3». Код вкладки остаётся (классический набор и
// прямые переходы _planV2GoToItem его не используют, но и не удалены).
const UI_SET_TABS = {
  classic: ['dash', 'plan', 'consumption', 'settings', 'reports'],
  // Партия 13, 13.2: «Настройки» (id 'settings') живут и в наборе v2.
  v2: ['overviewv2', 'reportsv2', 'structurev2', 'planv3', 'settings'],
};

// Партия 13, 13.4/13.5 (ТЗ §8.5, §10): запрос, не отвеченный за FEED_TIMEOUT_MS, считается
// потерянным (следующий тик начинает новый); живые данные, не обновлявшиеся дольше
// FEED_STALE_MS, на экране помечаются «устарело, обновлено ЧЧ:ММ».
const FEED_TIMEOUT_MS = 15000;
const FEED_STALE_MS = 30000;

// Партия 13, П2: ЕДИНЫЙ словарь типов элементов схемы. Одно слово — одно
// значение везде («Плана v3», «Структуры», «Обзора», отчётов); три копии подписей
// в разметке разошлись («Узел разветвления» / «Промежуточный узел» / «Узел»).
// Слово «узел» названием типа не служит. Менять подписи — только здесь.
const NODE_KINDS = [
  {value:'source', label:'Ввод',
   hint:'Откуда приходит питание (граница объекта).'},
  {value:'panel', label:'Щит',
   hint:'Распределяет питание: от него отходят линии.'},
  {value:'junction', label:'Ответвление',
   hint:'Точка разветвления без щита.'},
  {value:'load', label:'Нагрузка',
   hint:'Конечный потребитель.'},
];
const NODE_METER_NOTE =
  'Счётчик ставится на линию, а не на сам элемент: «+ Счётчик» спросит, какую линию он измеряет.';
const SCHEME_EMPTY_HINT =
  'Схема — это вводы, щиты и нагрузки, соединённые линиями. '+
  'Счётчик ставится на линию: тогда видно, сколько прошло по ней.';
// П11: x-show без каскада скрытия. Alpine 3.13.0 скрывает вложенные x-show
// через requestAnimationFrame и _x_hidePromise родителя. Если вложенный
// элемент успевает переключиться «скрыт → показан → скрыт», пока скрытие
// родителя ждёт кадра, он попадает в _x_hideChildren родителя дважды: второй
// проход берёт уже удалённый _x_hidePromise и падает «u is not a function»,
// а сам родитель остаётся на экране (так мигал test_b08 после «Удалить план»).
// x-transition в проекте не используется, поэтому каскад не нужен: x-show
// применяет значение сразу, итоговое состояние всегда равно последнему
// значению выражения. Вендорный alpine.min.js не правим.
document.addEventListener('alpine:init', ()=>{
  Element.prototype._x_toggleAndCascadeWithTransitions=
    function(el, value, show, hide){ if(value) show(); else hide(); };
});
function _planCssVar(name){
  try{
    const v=getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v||'#3dd6c0';
  }catch(e){ return '#3dd6c0'; }
}
function _planEscapeHtml(s){
  return String(s==null?'':s).replace(/[&<>"']/g, c=>(
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
}
function lsGet(k,def){ try{ return localStorage.getItem(k)||def; }catch(e){ return def; } }
function fmtTime(ts){
  return new Date(ts*1000).toLocaleString('ru-RU',
    {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
}
function fmtHour(ts){
  const d=new Date(ts*1000);
  return d.toLocaleString('ru-RU',
    {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
}

(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function corePart() {
    return {
      tab: (lsGet('wbem.uiSet', DEFAULT_UI_SET)=='v2') ? 'overviewv2' : 'dash',

      uiSet: lsGet('wbem.uiSet', DEFAULT_UI_SET),

      // П2: словарь типов — один на все списки (см. NODE_KINDS выше).
      nodeKinds: NODE_KINDS,

      nodeMeterNote: NODE_METER_NOTE,

      schemeEmptyHint: SCHEME_EMPTY_HINT,

      nodeKindLabel(kind){
          const k=NODE_KINDS.find(x=>x.value===kind);
          return k ? k.label : (kind||'');
        },

      nodeKindHint(kind){
          const k=NODE_KINDS.find(x=>x.value===kind);
          return k ? k.hint : '';
        },

      theme:lsGet('theme','dark'),

      loading:true,

      version:'',

      uptime:0,

      toast:null,

      _toastTimer:null,

      _timer:null,

      _initDone:false,

      init(){
          // Заглушка "интерфейс не запустился" (ТЗ v0.11.1, §3): раз init()
          // вообще выполнился — значит Alpine стартовал, заглушка не нужна.
          // Первым делом и до защиты от повторного вызова (повтор безвреден:
          // __wbemBootOk идемпотентен) — это проверяет test_step12_selfcheck.
          if(window.__wbemBootOk) window.__wbemBootOk();
          // Партия 13, 13.5 (§10): init() вызывается дважды — Alpine запускает его сам, а
          // x-init="init()" на <html> (нужен заглушке запуска) вызывает ещё раз; без защиты
          // заводились два таймера опроса, два обработчика Escape и т. д.
          if(this._initDone) return;
          this._initDone=true;
          // Партия 13, 13.1: брейкпоинты, viewport, панель карточки (layout.js).
          if(typeof this.initLayout==='function') this.initLayout();
          this.refresh();
          this.loadChannelDictionary();
          this.reloadGroups();
          // Если сохранённый выбор набора вкладок (localStorage, см. uiSet выше) —
          // новый интерфейс, начальная вкладка уже выставлена на 'overviewv2' —
          // догружаем её данные сразу, иначе экран будет пустым до первого тика
          // 5-секундного таймера ниже.
          if(this.tab=='overviewv2') this.openOverviewV2Tab();
          if(this.tab=='planv2') this.openPlanV2Tab();
          if(this.tab=='structurev2') this.openStructureTab();
          if(this.tab=='planv3') this.openPlanV3Tab();
          // Партия 13, 13.5 (§10): ОДИН общий тик 5 с; скрытая вкладка не опрашивает
          // сервер, а при возвращении делает ровно одно обновление (_pollTick).
          this._timer=setInterval(()=>{ if(!document.hidden) this._pollTick(); },5000);
          // При возврате: одно обновление сразу, а таймер заводится заново (всё равно один
          // живой таймер 5 с) — иначе очередной «обычный» тик мог прийти сразу вслед за
          // возвратным и дать второй запрос снимка подряд.
          document.addEventListener('visibilitychange', ()=>{
            if(document.hidden) return;
            clearInterval(this._timer);
            this._timer=setInterval(()=>{ if(!document.hidden) this._pollTick(); },5000);
            this._pollTick();
          });
          // Партия 6, баг из браузера (§5: "рисование линии/контура никогда не
          // завершается" — режим рисования оставался включённым). Escape —
          // безусловный аварийный выход из ЛЮБОГО активного режима рисования
          // geoman на обеих картах (v1-зона и v2-линия связи), независимо от
          // того, сработал ли у geoman finishOn:'dblclick' (см. startZoneDraw/
          // planV2StartDrawWaypoints — там же теперь отключается
          // doubleClickZoom на время рисования, см. те же места).
          document.addEventListener('keydown', (ev)=>{
            if(ev.key!=='Escape') return;
            // Партия 13, 13.6: если Escape отменил режим рисования/инструмент
            // карты — это всё, что он делает; иначе закрывает карточку/панель.
            const wasBusy=!!(this.planDrawingGroupId!==null || this.planV2DrawingEdgeKey!==null ||
                             this.planV2PlacingPolygon || this.planV3Tool);
            if(this.planDrawingGroupId!==null) this.cancelZoneDraw();
            if(this.planV2DrawingEdgeKey!==null) this.planV2CancelDrawWaypoints();
            if(this.planV2PlacingPolygon) this.planV2CancelPlacePolygon();
            // «План v3» (§0 задания, тот же баг класса "рисование не
            // заканчивается"): Escape — безусловный выход из любого активного
            // инструмента карты (узел/линия/зона), включая рисование контура
            // зоны geoman.
            if(this.planV3Tool) this.planV3CancelTool();
            if(!wasBusy && typeof this.layoutEscape==='function') this.layoutEscape(ev);
          });
        },

      async loadChannelDictionary(){
          try{
            const r=await fetch('/api/channels/dictionary');
            const d=await r.json();
            this.channelCategories=d.categories||[];
          }catch(e){ console.error(e); }
        },

      toggleTheme(){ this.theme=this.theme=='dark'?'light':'dark';
          try{localStorage.setItem('theme',this.theme)}catch(e){}
          // Цвета связей на плане берутся из CSS-переменных темы (--ok/--warn/
          // --err/--accent2) в момент отрисовки, не через var() внутри SVG —
          // после смены темы их нужно пересчитать явно.
          this.$nextTick(()=>{ if(typeof this._planApplyLiveStyles==='function') this._planApplyLiveStyles(); });
        },

      toggleUiSet(){
          this.uiSet = this.uiSet=='classic' ? 'v2' : 'classic';
          try{ localStorage.setItem('wbem.uiSet', this.uiSet); }catch(e){}
          // Активная вкладка принадлежала старому набору — она невидима в новом,
          // переезжаем на первую вкладку набора, куда переключились, и догружаем
          // для неё данные (та же логика, что и у клика по кнопке вкладки в nav).
          const tabs = UI_SET_TABS[this.uiSet] || [];
          if(tabs.indexOf(this.tab)===-1){
            this.tab = tabs[0];
            if(this.tab=='overviewv2') this.openOverviewV2Tab();
            else if(this.tab=='planv2') this.openPlanV2Tab();
            else if(this.tab=='reportsv2') this.openReportsV2Tab();
            else if(this.tab=='structurev2') this.openStructureTab();
            else if(this.tab=='planv3') this.openPlanV3Tab();
            else if(this.tab=='plan') this.openPlanTab();
            else if(this.tab=='consumption') this.loadConsumption();
            else if(this.tab=='settings'){ this.loadSettings(); this.initUpdatePanel(); }
            else if(this.tab=='reports') this.initReports();
          }
        },

      refresh(){
          this.loadStatus();
          if(this.tab=='consumption') this.loadConsumption();
          if(this.tab=='settings'){ this.loadSettings(); this.initUpdatePanel(); }
        },

      async loadStatus(poll){
          // Партия 13, 13.5 (§10): poll=true — вызов общего тика: пока предыдущий запрос
          // «/api/status» не завершён, новый не начинается. Явный вызов (после действия
          // пользователя) обесценивает идущий запрос и берёт свежее состояние.
          const t=this.feedStart('status', {live:true, skipIfBusy:poll===true});
          if(!t) return;
          try{
            const r=await fetch('/api/status', {signal:t.signal}); const d=await r.json();
            if(!this.feedLive(t)) return;
            this.meters=d.meters||[]; this.byStatus=d.meters_by_status||{};
            this.mqtt=d.mqtt||this.mqtt; this.version=d.version; this.uptime=d.uptime_s||0;
            this.feedDone(t,'ready');
          }catch(e){
            if(this.feedAborted(e) || !this.feedLive(t)) return;
            console.error(e); this.feedFail(t, e.message);
          }
          if(this.feedLive(t)) this.loading=false;
        },

      zoneColor(zone){
          // Принимает либо имя зоны строкой, либо объект зоны {name,color}
          // (§3.7 ТЗ). Приоритет — цвет из БД, иначе детерминированный хеш
          // от имени (фолбэк для старых зон без цвета — ничего не ломается).
          const name = typeof zone==='string' ? zone : ((zone&&zone.name)||'');
          let color = (zone && typeof zone==='object') ? zone.color : null;
          if(!color){
            const z=(this.zones||[]).find(zz=>zz.name===name);
            if(z) color=z.color;
          }
          if(color) return color;
          if(name==='— Без зоны —') return 'var(--txt3)';
          // Детерминированный цвет из имени зоны
          let h=0;
          for(let i=0;i<name.length;i++) h=(h*31+name.charCodeAt(i))%360;
          return `hsl(${h},55%,45%)`;
        },

      updateAgeClass(age){
          if(age==null) return 'muted';
          if(age<120) return 'cf-item cf-ok';
          if(age<600) return 'cf-item cf-warn';
          return 'cf-item cf-err';
        },

      fmtAge(age){
          if(age==null) return '—';
          if(age<60) return Math.round(age)+'с назад';
          if(age<3600) return Math.round(age/60)+'мин назад';
          return (age/3600).toFixed(1)+'ч назад';
        },

      statusColor(st){ return {
          ok:'var(--ok)',warning:'var(--warn)',no_measurement:'var(--idle)',
          incomplete:'var(--warn)',no_connection:'var(--err)',
          device_error:'var(--err)',unknown:'var(--txt3)',never_seen:'var(--txt3)',
          unconfirmed:'var(--info)'}[st]||'var(--txt3)';
        },

      statusLabel(st){ return {
          ok:'OK',warning:'Внимание',no_measurement:'Нет нагрузки',
          incomplete:'Нет фазы',no_connection:'Нет связи',
          device_error:'Ошибка',unknown:'—',never_seen:'Не подключалось',
          unconfirmed:'Не подтверждено'}[st]||st;
        },

      badgeStyle(st){
          const c=this.statusColor(st);
          return `background:${c}22;color:${c};border:1px solid ${c}55`;
        },

      qualityLabel(q){ return {ok:'OK',edge_approx:'~прибл.',gap:'разрыв',
          reset:'сброс',no_data:'нет данных',stale:'нет обновл.',sparse:'только целиком'}[q]||q; },

      qualityStyle(q){
          const c={ok:'var(--ok)',edge_approx:'var(--info)',gap:'var(--warn)',
            reset:'var(--err)',no_data:'var(--idle)',stale:'var(--idle)',sparse:'var(--info)'}[q]||'var(--txt3)';
          return `background:${c}22;color:${c}`;
        },

      fmt(v,d){
          if(v==null||v===undefined||isNaN(v)) return '—';
          return Number(v).toFixed(d);
        },

      channelLabel(name){
          const c=this.detail&&this.detail.controls&&this.detail.controls[name];
          const label=(c&&c.label)?c.label:name;
          const units=(c&&c.units)?(' ('+c.units+')'):'';
          return label+units;
        },

      _apiErrorMessage(d, status){
          if(d && d.code==='revision_conflict'){
            return 'Конфигурацию кто-то изменил, пока была открыта форма — '+
                   'ничего не перезаписано. Обновите форму и повторите: '+(d.message||'');
          }
          return (d && d.message) || ('HTTP '+status);
        },

      // Тело общего тика (setInterval в init() и возврат на видимую вкладку): что обновлять на
      // текущем экране. Новых таймеров не заводить (§10); каждый загрузчик ниже сам не
      // начинает запрос, пока предыдущий того же типа не завершён (feedStart skipIfBusy).
      _pollTick(){
          this.feedNow=Date.now();
          this._pollN=(this._pollN||0)+1;
          if(this.tab=='dash') this.loadStatus(true);
          // Шапка нового набора (MQTT, версия) обновляется реже — раз в 30 с.
          else if(this.uiSet=='v2' && this._pollN%6===0) this.loadStatus(true);
          // План (ТЗ v0.11.0): та же частота 5с, что и дашборд. Обновляем
          // только стили/попапы (_planApplyLiveStyles), слои не пересоздаём —
          // иначе открытый попап зоны закрывался бы на каждый тик.
          if(this.tab=='plan') this.loadPlanLive(true);
          if(this.tab=='overviewv2') this.loadOverviewV2Snapshot();
          // Партия 7, Этап 3, Э3/B7: "новый таймер не заводить" — P-now по
          // линиям открытой карточки узла/тока-загрузки открытой карточки
          // линии на «Плане v3» обновляются тем же тиком.
          if(this.tab=='planv3') this._planV3PollLiveCard();
          // Партия 13, П6: «Структура» — дерево схемы с текущей мощностью
          // из того же снимка, что и «Обзор»; тот же тик (5с), карточка
          // узла обновляет P-now линий так же, как на «Плане v3».
          if(this.tab=='structurev2'){
            this._loadStructureSnapshot();
            this._planV3PollLiveCard();
          }
        },

      _pollN:0,

      // ===== Партия 13, 13.4: «ленты» данных — общий хелпер последовательности запросов =====
      // Одна лента — один тип запроса экрана (ключ: 'ov-snap', 'rv2-query', 'inspector', …).
      // feedStart(key) обесценивает предыдущий запрос ленты (A33): его ответ, пришедший позже,
      // экран не перетирает (feedLive(token) === false), а сам запрос отменяется (AbortController),
      // поэтому на клиента приходится ОДИН незавершённый запрос каждого типа (§10). Для
      // периодического опроса feedStart(key,{live:true,skipIfBusy:true}): пока предыдущий запрос
      // ленты не завершён (и не просрочен), новый не начинается. Образец, который обобщён, —
      // _structSearchSeq поиска «Структуры».
      // Состояние для экрана: feeds[key] = {loading, status:'ready'|'empty'|'partial'|'error'|null,
      // updatedAt (мс последнего успешного ответа), error, hasData}; feedStale(key)/feedStaleText(key)
      // дают признак «устарело, обновлено ЧЧ:ММ»: ответ не пришёл (error) либо у живой ленты
      // нет свежего ответа дольше FEED_STALE_MS. Прежние данные при этом остаются на экране.
      feeds:{},

      feedNow:Date.now(),

      _feedSeq:{},

      _feedAbort:{},

      _feedStartedAt:{},

      _feedLiveKeys:{},

      feedStart(key, opts){
          opts=opts||{};
          const cur=this.feeds[key]||{};
          if(opts.skipIfBusy && cur.loading &&
             (Date.now()-(this._feedStartedAt[key]||0))<FEED_TIMEOUT_MS) return null;
          const prev=this._feedAbort[key];
          if(prev){ try{ prev.abort(); }catch(e){ /* уже завершён */ } }
          const ctl=(typeof AbortController==='function') ? new AbortController() : null;
          this._feedAbort[key]=ctl;
          const n=(this._feedSeq[key]||0)+1;
          this._feedSeq[key]=n;
          this._feedStartedAt[key]=Date.now();
          if(opts.live) this._feedLiveKeys[key]=true;
          this.feeds={...this.feeds, [key]:{loading:true, status:cur.status||null,
            updatedAt:cur.updatedAt||null, error:cur.error||null, hasData:!!cur.hasData}};
          return {key, n, signal:ctl?ctl.signal:undefined};
        },

      feedLive(t){ return !!t && this._feedSeq[t.key]===t.n; },

      feedAborted(e){ return !!e && (e.name==='AbortError'); },

      feedDone(t, kind){
          if(!this.feedLive(t)) return false;
          kind=kind||'ready';
          this.feeds={...this.feeds, [t.key]:{loading:false, status:kind, updatedAt:Date.now(),
            error:null, hasData:kind!=='empty'}};
          return true;
        },

      feedFail(t, message){
          if(!this.feedLive(t)) return false;
          const cur=this.feeds[t.key]||{};
          this.feeds={...this.feeds, [t.key]:{loading:false, status:'error',
            updatedAt:cur.updatedAt||null, error:message||'нет ответа', hasData:!!cur.hasData}};
          return true;
        },

      // Забыть данные ленты (например, сменился запрос, и прежние данные — про другое):
      // идущий запрос обесценивается, признак устаревания и «обновлено» сбрасываются.
      feedReset(key){
          const prev=this._feedAbort[key];
          if(prev){ try{ prev.abort(); }catch(e){ /* уже завершён */ } }
          this._feedSeq[key]=(this._feedSeq[key]||0)+1;
          const f={...this.feeds}; delete f[key]; this.feeds=f;
        },

      feedLoading(key){ return !!(this.feeds[key]||{}).loading; },

      feedStatus(key){ return (this.feeds[key]||{}).status||null; },

      feedError(key){ return (this.feeds[key]||{}).error||null; },

      feedStale(key){
          const f=this.feeds[key];
          if(!f || !f.updatedAt || !f.hasData) return false;
          if(f.error) return true;
          return !!this._feedLiveKeys[key] && (this.feedNow-f.updatedAt)>FEED_STALE_MS;
        },

      feedStaleText(key){
          const f=this.feeds[key];
          if(!f || !f.updatedAt) return '';
          const t=new Date(f.updatedAt).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
          return 'устарело, обновлено '+t;
        },

      showToast(msg, ok=true){
          clearTimeout(this._toastTimer);
          this.toast={msg,ok};
          this._toastTimer=setTimeout(()=>{ this.toast=null; },3000);
        }
    };
  });
})();

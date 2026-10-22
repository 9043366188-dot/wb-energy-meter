// overview-v2.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
// ---- Партия 15, П13: раздел «Приборы» (карточки приборов из MQTT) ----
// Порядок карточек: сначала приборы с проблемами, затем остальные. Ранг — по статусу
// прибора из /api/status; статус, которого здесь нет, считается проблемой (виден первым).
const OV_DEV_RANK = {device_error:0, no_connection:1, incomplete:2, warning:3,
                     unconfirmed:4, unknown:5, no_measurement:6, ok:7};
const OV_DEV_PROBLEM_MAX_RANK = 5;
// Слова сводки «6 приборов: 4 в норме, 1 ошибка, 1 нет нагрузки».
const OV_DEV_SUMMARY_ORDER = ['ok', 'device_error', 'no_connection', 'incomplete', 'warning',
                              'unconfirmed', 'unknown', 'no_measurement'];
function ovPlural(n, one, few, many){
  const m10 = n % 10, m100 = n % 100;
  if(m10 === 1 && m100 !== 11) return one;
  if(m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}
function ovDevSummaryWord(status, n){
  switch(status){
    case 'ok': return 'в норме';
    case 'device_error': return ovPlural(n, 'ошибка', 'ошибки', 'ошибок');
    case 'no_connection': return 'нет связи';
    case 'no_measurement': return 'нет нагрузки';
    case 'incomplete': return 'нет фазы';
    case 'warning': return 'внимание';
    case 'unconfirmed': return 'не подтверждено';
    case 'unknown': return 'состояние неизвестно';
    default: return String(status);
  }
}

(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function overviewV2Part() {
    return {
      overviewV2Inited:false,

      overviewV2Loading:false,

      overviewV2Error:null,

      overviewV2Snapshot:null,

      overviewV2Groups:[],

      overviewV2GroupMembers:{},

      overviewV2GroupsError:null,

      ovFrom:null,

      ovTo:null,

      ovLoading:false,

      ovError:null,

      ovSummary:null,

      ovPointNames:{},

      ovDisclosureOpen:{},

      validationResult:null,

      validationLoading:false,

      validationError:null,

      async openOverviewV2Tab(){
          if(!this.overviewV2Inited){
            this.overviewV2Inited=true;
            const loads=[this.loadOverviewV2Snapshot(), this.loadOverviewV2Groups(),
                         this.initOvAccounting(), this.loadValidation()];
            // Э3/B10: «Требует внимания» не должен молчать про приборы, которые
            // вообще ещё не стали точкой учёта (раньше считались только
            // ПРОБЛЕМЫ УЖЕ ЗАВЕДЁННЫХ точек, overviewV2Problems() ниже) — нужны
            // structPoints (meter_device_id занятых точек) и структурный список
            // устройств (_loadKnownDevices), тот же, что у «Плана v3»
            // (planV3DevicesWithoutPoint() переиспользуется как есть).
            if(!this.structInited){ this.structInited=true; loads.push(this._loadStructureLists()); }
            if(!this.structKnownDevicesLoaded) loads.push(this._loadKnownDevices());
            loads.push(this.loadEmulatorSummary());
            // П13: «Приборы» — свой запрос /api/status (лента 'status'), не зависящий от точек и схемы.
            loads.push(this.loadStatus());
            await Promise.all(loads);
          }else{
            // Партия 13 (П5): после «+ Счётчик» на другой вкладке «Обзор» должен
            // сразу показать новую точку, а не ждать следующего тика таймера.
            await Promise.all([this.loadOverviewV2Snapshot(), this.loadValidation(),
                               this.loadEmulatorSummary(), this.loadStatus()]);
          }
        },

      async loadValidation(){
          // §8.2/§13 (партия 3, задача 4): сводка незавершённой настройки —
          // сервер уже отсортировал issues по влиянию (no_meter впереди no_plan
          // и т.п., см. _VALIDATION_IMPACT_RANK в api_v2.py), на фронте порядок
          // не трогаем. Необязательно для остального Обзора при ошибке.
          this.validationLoading=true; this.validationError=null;
          try{
            const r=await fetch('/api/v2/validation');
            if(!r.ok) throw new Error(`HTTP ${r.status}`);
            const v=await r.json();
            // Партия 13, П6 (решение R11): места в наборе v2 нигде не показываются —
            // проверка «нет места» из сводки убрана (сервер её по-прежнему
            // считает для классического набора и API).
            v.issues=(v.issues||[]).filter(i=>i.kind!=='no_location');
            v.total_issues=v.issues.length;
            this.validationResult=v;
          }catch(e){
            this.validationError='Не удалось получить сводку конфигурации: '+e.message;
          }finally{
            this.validationLoading=false;
          }
        },

      validationIssueLabel(kind){
          return {
            no_meter:'нет прибора', no_group:'нет группы',
            no_plan:'не на плане', node_without_edges:'элемент не подключён к линиям',
            edge_without_measurement:'линия без счётчика',
          }[kind] || kind;
        },

      async loadOverviewV2Groups(){
          // Корневые группы (parent_id=null) + их ЭФФЕКТИВНЫЙ состав
          // (GroupRepoV2.resolve_effective_members на бэкенде -- сама группа +
          // все дочерние, дедуплицировано по точке). Не критично для остального
          // экрана: при ошибке просто не показываем таблицу веток, снимок и
          // KPI-плитки работают независимо от групп.
          try{
            const r=await fetch('/api/v2/groups?parent_id=null');
            if(!r.ok) throw new Error(`HTTP ${r.status}`);
            const groups=await r.json();
            const members={};
            await Promise.all(groups.map(async g=>{
              const rm=await fetch(`/api/v2/groups/${g.id}/effective-members`);
              if(!rm.ok) throw new Error(`HTTP ${rm.status} (группа ${g.id})`);
              const dm=await rm.json();
              members[g.id]=new Set(dm.points.map(p=>p.point_id));
            }));
            this.overviewV2Groups=groups;
            this.overviewV2GroupMembers=members;
            this.overviewV2GroupsError=null;
          }catch(e){
            this.overviewV2Groups=[];
            this.overviewV2GroupMembers={};
            this.overviewV2GroupsError=`Не удалось загрузить группы: ${e.message}`;
          }
        },

      overviewV2BranchRows(){
          // ТЗ §8.2 "Распределение 1 уровня": по каждой корневой группе -- сумма
          // ТЕКУЩЕГО снимка (не период-баланс) по её эффективному составу.
          // ВАЖНО: это информационная разбивка по веткам, а не компоненты
          // проверенного итога объекта -- ветки могут пересекаться по точкам
          // (ТЗ §4.4 разрешает одной точке состоять в нескольких группах,
          // например в дереве категории "арендаторы" И "производство"
          // одновременно), поэтому сумма строк таблицы НЕ обязана и не должна
          // совпадать с overviewV2TotalPowerKw()/overviewV2TotalEnergyKwh() --
          // те считаются по ВСЕМ точкам снимка независимо от групп. Здесь та же
          // null-vs-0 защита (A10), что и в overviewV2TotalPowerKw().
          if(!this.overviewV2Snapshot || !this.overviewV2Snapshot.points) return [];
          const byId={};
          for(const p of this.overviewV2Snapshot.points) byId[p.point_id]=p;
          return this.overviewV2Groups.map(g=>{
            const memberIds=this.overviewV2GroupMembers[g.id]||new Set();
            const pts=[...memberIds].map(id=>byId[id]).filter(Boolean);
            const withPower=pts.filter(p=>p.power_w!=null);
            const withEnergy=pts.filter(p=>p.energy_total_kwh!=null);
            return {
              group:g,
              pointCount:pts.length,
              has_emulator:pts.some(p=>!!p.is_emulator),
              knownCount:withPower.length,
              power_kw: withPower.length>0 ? withPower.reduce((s,p)=>s+p.power_w,0)/1000 : null,
              energy_kwh: withEnergy.length>0 ? withEnergy.reduce((s,p)=>s+p.energy_total_kwh,0) : null,
            };
          });
        },

      overviewV2UngroupedCount(){
          // Точки снимка, не входящие НИ В ОДНУ корневую группу (по любому
          // пути) -- индикатор полноты структуры, не "небаланс" (для него
          // нужен проверенный balance-scope, не просто сумма по составу).
          if(!this.overviewV2Snapshot || !this.overviewV2Snapshot.points) return 0;
          const grouped=new Set();
          for(const g of this.overviewV2Groups){
            const ids=this.overviewV2GroupMembers[g.id];
            if(ids) for(const id of ids) grouped.add(id);
          }
          return this.overviewV2Snapshot.points.filter(p=>!grouped.has(p.point_id)).length;
        },

      async loadOverviewV2Snapshot(){
          // Партия 13, 13.4/13.5: одна лента 'ov-snap' — пока предыдущий запрос не завершён,
          // новый тик не начинает свой; при обрыве прежний снимок остаётся на экране с
          // признаком «устарело, обновлено ЧЧ:ММ» (feedStale), а не заменяется ошибкой.
          const t=this.feedStart('ov-snap', {live:true, skipIfBusy:true});
          if(!t) return;
          const shouldShowLoading=!this.overviewV2Snapshot;
          if(shouldShowLoading) this.overviewV2Loading=true;
          try{
            const r=await fetch('/api/v2/snapshot', {signal:t.signal});
            if(!r.ok){ throw new Error(`HTTP ${r.status}`); }
            const d=await r.json();
            if(!this.feedLive(t)) return;
            this.overviewV2Snapshot=d;
            this.overviewV2Error=null;
            this.feedDone(t, (d.points||[]).length===0 ? 'empty' :
              (this.overviewV2PartialText()!=='' ? 'partial' : 'ready'));
          }catch(e){
            if(this.feedAborted(e) || !this.feedLive(t)) return;
            this.overviewV2Error=`Не удалось загрузить снимок: ${e.message}`;
            this.feedFail(t, e.message);
          }finally{
            if(this.feedLive(t)) this.overviewV2Loading=false;
          }
        },

      // «Частично»: у части точек снимка нет значения мощности — суммы плиток неполные.
      overviewV2PartialText(){
          const pts=(this.overviewV2Snapshot||{}).points||[];
          const missing=pts.filter(p=>p.power_w==null).length;
          if(!pts.length || missing===0) return '';
          return `Нет данных по ${missing} из ${pts.length} точек — суммы неполные.`;
        },

      overviewV2TotalPowerKw(){
          // ТЗ A10: точка с настоящим нулевым чтением ("исправна, но нагрузки
          // нет") — это 0, а не "нет данных". Отличаем по наличию хотя бы
          // одного НЕ-null значения, а не по знаку суммы (сумма из одних
          // истинных нулей тоже 0, и это не то же самое, что "нечего сложить").
          if(!this.overviewV2Snapshot || !this.overviewV2Snapshot.points) return null;
          const known=this.overviewV2Snapshot.points.filter(p=>p.power_w!=null);
          if(known.length===0) return null;
          return known.reduce((sum,p)=>sum+p.power_w, 0)/1000;
        },

      overviewV2TotalEnergyKwh(){
          if(!this.overviewV2Snapshot || !this.overviewV2Snapshot.points) return null;
          const known=this.overviewV2Snapshot.points.filter(p=>p.energy_total_kwh!=null);
          if(known.length===0) return null;
          return known.reduce((sum,p)=>sum+p.energy_total_kwh, 0);
        },

      // ===== Партия 15, П13: раздел «Приборы» =====
      // Карточка на КАЖДЫЙ прибор из MQTT (`meters` из /api/status — тот же источник, что у
      // классического «Дашборда»), независимо от точек учёта, схемы, привязок и итога объекта.
      // Состояние запроса — лента 'status' (feedStale/feedError): сбой снимка или итога не
      // прячет приборы, а сбой самого запроса оставляет прежние карточки с пометкой «устарело».

      // Точки учёта прибора — из снимка «Обзора» (поле device_id). Нет снимка — состояние
      // неизвестно, это не «не привязан».
      ovDevicePointState(deviceId){
          const snap=this.overviewV2Snapshot;
          if(!snap || !snap.points){
            return {kind:'unknown',
                    text:this.overviewV2Loading ? 'Точка учёта: определяется…' : 'Точка учёта: не удалось определить'};
          }
          const pts=snap.points.filter(p=>p.device_id===deviceId);
          if(!pts.length) return {kind:'free', text:'Точка учёта: не привязан'};
          const phase=p=>({phase_l1:'L1', phase_l2:'L2', phase_l3:'L3'})[p.channel_profile]||'';
          const names=pts.map(p=>'«'+(p.name||p.code)+'»'+(phase(p) ? ' · '+phase(p) : ''));
          return {kind:'bound',
                  text:(pts.length>1 ? 'Точки учёта: ' : 'Точка учёта: ')+names.join(', ')};
        },

      // Строки раздела: сначала приборы с проблемами (ошибка, нет связи…), затем остальные;
      // внутри ранга — по имени.
      ovDeviceRows(){
          const rank=st=>(st in OV_DEV_RANK) ? OV_DEV_RANK[st] : OV_DEV_PROBLEM_MAX_RANK;
          const name=m=>String(m.display_name||m.device_id||'');
          return (this.meters||[]).slice()
            .sort((a,b)=>rank(a.status)-rank(b.status) || name(a).localeCompare(name(b),'ru'))
            .map(m=>({m, problem:rank(m.status)<=OV_DEV_PROBLEM_MAX_RANK,
                      point:this.ovDevicePointState(m.device_id)}));
        },

      // Сводка в заголовке: «6 приборов: 4 в норме, 1 ошибка, 1 нет нагрузки».
      ovDevicesSummaryText(){
          const ms=this.meters||[];
          if(!ms.length) return '';
          const count={};
          ms.forEach(m=>{ count[m.status]=(count[m.status]||0)+1; });
          const known=OV_DEV_SUMMARY_ORDER.filter(st=>count[st]);
          const other=Object.keys(count).filter(st=>OV_DEV_SUMMARY_ORDER.indexOf(st)===-1);
          const parts=known.concat(other).map(st=>count[st]+' '+ovDevSummaryWord(st, count[st]));
          return ms.length+' '+ovPlural(ms.length,'прибор','прибора','приборов')+': '+parts.join(', ');
        },

      // Мощность прибора в кВт для карточки. Классический «Дашборд» подписывает Total P как «кВт»;
      // эмулятор (и часть приборов) публикуют её в ваттах и сообщают это в meta ("W"). Если meta
      // прибора явно говорит «Вт» — переводим в кВт, иначе значение уже в кВт, как в классике.
      ovDevicePowerKw(m){
          const v=m&&m.main_values?m.main_values['Total P']:null;
          if(typeof v!=='number'||isNaN(v)) return null;
          const u=String(m.power_units||'').trim().toLowerCase();
          return (u==='w'||u==='вт')?v/1000:v;
        },

      // Свободный прибор → «+ Счётчик»: ставится в «Структуре» («Не на схеме» → «Приборы без
      // точки учёта» → «+ Счётчик → куда?»), там же выбирается элемент схемы.
      ovDeviceAddMeter(){
          this.tab='structurev2'; this.openStructureTab();
        },

      overviewV2Problems(){
          if(!this.overviewV2Snapshot || !this.overviewV2Snapshot.points) return [];
          const probs=[];
          for(const p of this.overviewV2Snapshot.points){
            if(p.binding_status==='unbound'){
              probs.push({point:p, kind:'unbound'});
            }else if(['never_seen','no_connection','device_error'].includes(p.device_status)){
              probs.push({point:p, kind:p.device_status});
            }
          }
          return probs;
        },

      _ovDefaultDates(){
          // По умолчанию — с начала текущего месяца по сегодня (без сегодняшнего
          // дня целиком, т.к. `to` — конец полуоткрытого периода [from,to)).
          const today=new Date();
          const first=new Date(today.getFullYear(), today.getMonth(), 1);
          const fmt=d=>d.toISOString().slice(0,10);
          return {from: fmt(first), to: fmt(today)};
        },

      async initOvAccounting(){
          if(!this.ovFrom || !this.ovTo){
            const d=this._ovDefaultDates();
            this.ovFrom=d.from; this.ovTo=d.to;
          }
          await this._loadOvPointNames();
          await this.loadOvSummary();
        },

      async _loadOvPointNames(){
          // Имена точек для disclosure/раздельных строк при конфликте (A04) —
          // не критично для остального блока, при ошибке просто покажем "#id".
          if(Object.keys(this.ovPointNames).length>0) return;
          try{
            const r=await fetch('/api/v2/points');
            if(!r.ok) return;
            const pts=await r.json();
            const names={};
            (pts||[]).forEach(p=>{ names[p.id]=p.name||p.code; });
            this.ovPointNames=names;
          }catch(e){ /* необязательно */ }
        },

      ovPointName(id){ return this.ovPointNames[id] || ('#'+id); },

      async loadOvSummary(){
          // Даты <input type=date> ("YYYY-MM-DD") принимаются backend'ом
          // напрямую (periods.py::parse_user_datetime) — конвертация в unix
          // делается на сервере, здесь не нужна.
          if(!this.ovFrom || !this.ovTo) return;
          // Оба поля дат независимые (у каждого свой @change="loadOvSummary()"),
          // поэтому при редактировании ovFrom/ovTo по очереди на мгновение
          // возможно временное from>to (второе поле ещё не подтянулось) —
          // сервер честно отвечает 400 (periods.py::build_period: "ts_to
          // должен быть больше ts_from"), но это не ошибка пользователя, а
          // промежуточное состояние формы. Не дёргаем сервер и не показываем
          // "не удалось посчитать" — подождём, когда оба поля устаканятся
          // (второй @change перезапустит расчёт с уже верным диапазоном).
          if(this.ovFrom > this.ovTo) return;
          // Партия 13, 13.4 (A33): каждый новый расчёт обесценивает предыдущий — запоздавший
          // ответ СТАРОГО периода не перетирает новый (лента 'ov-summary').
          const key=this.ovFrom+'|'+this.ovTo;
          const t=this.feedStart('ov-summary');
          this.ovLoading=true; this.ovError=null;
          try{
            const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
            const r=await fetch('/api/v2/overview/summary', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body:JSON.stringify({from:this.ovFrom, to:this.ovTo, timezone:tz}),
              signal:t.signal,
            });
            const d=await r.json();
            if(!this.feedLive(t)) return;
            if(!r.ok){ throw new Error(d.message||`HTTP ${r.status}`); }
            this.ovSummary=d;
            this._ovSummaryKey=key;
            // Партия 5, задача 3 (§8.5): различать ПОЧЕМУ у точки в режиме
            // сравнения (electrическое пересечение группы) нет числа — раньше
            // везде было одно "нет данных" независимо от причины.
            if((d.branches||[]).some(b=>b.mode==='comparison')) await this._loadOvDataStateHelpers();
            this.feedDone(t, this._ovSummaryKind(d));
          }catch(e){
            if(this.feedAborted(e) || !this.feedLive(t)) return;
            this.ovError=`Не удалось посчитать итог за период: ${e.message}`;
            // Прежний итог ТОГО ЖЕ периода остаётся на экране с признаком «устарело»;
            // итог другого периода выдавать за текущий нельзя.
            if(this._ovSummaryKey!==key) this.ovSummary=null;
            this.feedFail(t, e.message);
          }finally{
            if(this.feedLive(t)) this.ovLoading=false;
          }
        },

      _ovSummaryKey:null,

      // Состояние итога по §8.5: empty — ввод не назначен (считать нечего), partial — итог
      // неполный (не все вводы/ветви измеряются), ready — посчитан целиком.
      _ovSummaryKind(d){
          if(!d.object_total && d.object_total_unavailable_reason==='no_input_assigned') return 'empty';
          if((d.unmetered_inputs||[]).length>0 || d.boundary_coverage==='has_unmetered_branches' ||
             (d.object_total && d.object_total.value==null)) return 'partial';
          return 'ready';
        },

      async _loadOvDataStateHelpers(){
          // snapshot даёт "нет прибора" (binding_status==='unbound') и "нет
          // связи" (устройство не отвечает/устарело); archived_at из
          // points?include_archived=1 даёт "архив". Best-effort — если что-то
          // не загрузилось, соответствующие точки просто останутся с общим
          // "нет данных" (не хуже, чем было).
          try{
            const [sr, pr] = await Promise.all([
              fetch('/api/v2/snapshot'), fetch('/api/v2/points?include_archived=1'),
            ]);
            this._ovSnapshotById = {};
            if(sr.ok){ (await sr.json()).points.forEach(p=>{ this._ovSnapshotById[p.point_id]=p; }); }
            this._ovArchivedIds = new Set();
            if(pr.ok){ (await pr.json()).forEach(p=>{ if(p.archived_at!=null) this._ovArchivedIds.add(p.id); }); }
          }catch(e){ /* см. комментарий выше — best-effort */ }
        },

      ovPointDataStateLabel(pid){
          if((this._ovArchivedIds||new Set()).has(pid)) return 'архив';
          const snap = (this._ovSnapshotById||{})[pid];
          if(!snap) return 'нет данных';
          if(snap.binding_status==='unbound') return 'нет прибора';
          if(['no_connection','never_seen','unknown'].includes(snap.device_status)) return 'нет связи';
          return 'нет данных';
        },

      ovToggleDisclosure(key){
          this.ovDisclosureOpen={...this.ovDisclosureOpen, [key]: !this.ovDisclosureOpen[key]};
        },

      ovGoConfigureBoundary(){
          // §8.2: "без назначенного ввода — действие 'Настроить границу
          // объекта'". Партия 5: раньше вело на План v2 — но там нельзя
          // завести НОВЫЙ узел/связь, только разместить уже существующие (см.
          // §0 ТЗ партии 5). Теперь ведёт прямо на форму создания узла на
          // «Структуре», где сеть заводится с нуля.
          this.tab='structurev2'; this.openStructureTab();
          this.openCreatePanel('node');
        }
    };
  });
})();

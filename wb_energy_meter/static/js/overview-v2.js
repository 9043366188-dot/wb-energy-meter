// overview-v2.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
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
            await Promise.all(loads);
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
            this.validationResult=await r.json();
          }catch(e){
            this.validationError='Не удалось получить сводку конфигурации: '+e.message;
          }finally{
            this.validationLoading=false;
          }
        },

      validationIssueLabel(kind){
          return {
            no_meter:'нет прибора', no_location:'нет места', no_group:'нет группы',
            no_plan:'не размещена на плане', node_without_edges:'узел изолирован',
            edge_without_measurement:'связь без измерения',
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
          const shouldShowLoading=!this.overviewV2Snapshot;
          if(shouldShowLoading) this.overviewV2Loading=true;
          try{
            const r=await fetch('/api/v2/snapshot');
            if(!r.ok){ throw new Error(`HTTP ${r.status}`); }
            const d=await r.json();
            this.overviewV2Snapshot=d;
            this.overviewV2Error=null;
          }catch(e){
            this.overviewV2Error=`Не удалось загрузить снимок: ${e.message}`;
          }finally{
            this.overviewV2Loading=false;
          }
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
          this.ovLoading=true; this.ovError=null;
          try{
            const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
            const r=await fetch('/api/v2/overview/summary', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body:JSON.stringify({from:this.ovFrom, to:this.ovTo, timezone:tz}),
            });
            const d=await r.json();
            if(!r.ok){ throw new Error(d.message||`HTTP ${r.status}`); }
            this.ovSummary=d;
            // Партия 5, задача 3 (§8.5): различать ПОЧЕМУ у точки в режиме
            // сравнения (electrическое пересечение группы) нет числа — раньше
            // везде было одно "нет данных" независимо от причины.
            if((d.branches||[]).some(b=>b.mode==='comparison')) await this._loadOvDataStateHelpers();
          }catch(e){
            this.ovError=`Не удалось посчитать итог за период: ${e.message}`;
            this.ovSummary=null;
          }finally{
            this.ovLoading=false;
          }
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

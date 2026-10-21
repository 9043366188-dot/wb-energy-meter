// structure.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function structurePart() {
    return {
      migrateLegacyConfirm:false,

      migrateLegacyBusy:false,

      migrateLegacyResult:null,

      migrateLegacyError:null,

      structInited:false,

      structLoading:false,

      structError:null,

      structQuery:'',

      structPoints:[],

      structLocations:[],

      structGroups:[],

      structNodes:[],

      structEdges:[],

      structInspector:null,

      structInspectorLoading:false,

      structInspectorError:null,

      // Партия 12 (12.3): «История» карточки — журнал изменений
      // GET /api/v2/change-log для открытой в инспекторе сущности. Загружается
      // по кнопке (не на каждый клик по дереву), состояние живёт отдельно от
      // structInspector, чтобы перезагрузка карточки после правки его не теряла.
      structHistory:{open:false, loading:false, error:null, items:[], next:null,
                     startedAt:null, key:null},

      _structSearchResults:null,

      // Партия 13, 13.4: токен последней загрузки карточки (лента 'inspector', core.js) и
      // пометка «карточка загружена не целиком» (часть необязательных запросов не ответила).
      _inspTok:null,

      structInspectorPartial:null,

      structCreatePanel:null,

      structCreateSaving:false,

      structCreateError:null,

      structCreateOk:null,

      edgeForm:{from_node_id:null, to_node_id:null, primary_point_id:null,
                  rated_current_a:'', cable_note:'', name:''},

      structKnownDevices:[],

      structKnownDevicesLoaded:false,

      structDraftEdges:[],

      publishSelected:[],

      publishValidation:null,

      replaceMeterForm:null,

      async openStructureTab(){
          // Приборы для «+ Счётчик» и «Приборы без точки учёта» — нужны сразу.
          if(!this.structKnownDevicesLoaded) this._loadKnownDevices();
          // Повторный вход перечитывает схему: пока вы были на «Плане v3»,
          // она могла измениться.
          this.structInited=true;
          await this._loadStructureLists();
        },

      async _loadStructureLists(){
          this.structLoading=true; this.structError=null;
          try{
            const [pr, lr, gr, nr, er] = await Promise.all([
              fetch('/api/v2/structure/points'),
              fetch('/api/v2/locations'),
              fetch('/api/v2/groups'),
              fetch('/api/v2/topology/nodes'),
              fetch('/api/v2/topology/edges?state=published'),
            ]);
            if(!pr.ok) throw new Error('structure/points: HTTP '+pr.status);
            // ВНИМАНИЕ: из пяти ручек ниже четыре отдают СПИСОК, а
            // /api/v2/structure/points — ОБЪЕКТ {"points": [...]}. Присвоение
            // ответа как есть делало structPoints объектом, и клик по точке в
            // «Обзоре»/«Структуре» падал с «(intermediate value).find is not a
            // function» — карточка не открывалась вообще. Найдено пользователем
            // в браузере 18.09.2026; ниже, в _loadStructureSnapshot, то же поле
            // заполнялось правильно, поэтому расхождение и не бросалось в глаза.
            const spd = await pr.json();
            this.structPoints = spd.points || [];
            // П4: какие приборы заняты и чьей точкой — список выбора прибора
            // показывает только свободные, занятые — серым.
            this.structOccupiedDevices = spd.occupied_devices || [];
            this.structLocations = lr.ok ? await lr.json() : [];
            this.structGroups = gr.ok ? await gr.json() : [];
            this.structNodes = nr.ok ? await nr.json() : [];
            this.structEdges = er.ok ? await er.json() : [];
            await this._loadStructureSnapshot();
            if(this.structShowArchived) await this._loadArchivedPoints();
            if(this.tab==='structurev2') this._loadStructureTreeEnergy();
          }catch(e){
            this.structError = 'Не удалось загрузить структуру: '+e.message;
          }finally{
            this.structLoading=false;
          }
        },

      async _loadStructureSnapshot(){
          // Метрики центра/списка (мощность, статус) — из общего снимка,
          // ничего не пересчитывается здесь (см. модульный докстринг
          // v2_snapshot в api_v2.py).
          // Партия 13, 13.4/13.5: лента 'struct-snap' — один запрос за раз; при обрыве
          // прежние числа остаются с признаком «устарело, обновлено ЧЧ:ММ».
          const t=this.feedStart('struct-snap', {live:true, skipIfBusy:true});
          if(!t) return;
          try{
            const r=await fetch('/api/v2/snapshot', {signal:t.signal});
            if(!r.ok) throw new Error('HTTP '+r.status);
            const d=await r.json();
            if(!this.feedLive(t)) return;
            const byId = {};
            (d.points||[]).forEach(p=>{ byId[p.point_id]=p; });
            this._structSnapshotById = byId;
            this.feedDone(t, (d.points||[]).length ? 'ready' : 'empty');
          }catch(e){
            if(this.feedAborted(e) || !this.feedLive(t)) return;
            this._structSnapshotById = this._structSnapshotById||{};
            this.feedFail(t, e.message);
          }
        },

      structSnapshotFor(pointId){
          return (this._structSnapshotById||{})[pointId] || null;
        },

      async structureSearch(){
          // A02: поиск по имени/коду/MQTT ID/серийнику/пути через
          // /api/v2/structure/points?q= — сервер уже фильтрует и не прячет
          // непривязанные/неразмещённые точки. _structSearchSeq защищает от
          // устаревшего ответа, если пользователь печатает быстро.
          // Партия 13, 13.4: последовательность запросов — общий хелпер core.js
          // (лента 'struct-search'; бывший _structSearchSeq обобщён, а не продублирован).
          const q = this.structQuery.trim();
          const t = this.feedStart('struct-search');
          try{
            const r = await fetch('/api/v2/structure/points'+(q?('?q='+encodeURIComponent(q)):''),
                                  {signal:t.signal});
            if(!r.ok) throw new Error('HTTP '+r.status);
            const d = await r.json();
            if(!this.feedLive(t)) return;
            if(q){
              this._structSearchResults = d.points||[];
            }else{
              this.structPoints = d.points||[];
              this._structSearchResults = null;
              await this._loadStructureSnapshot();
            }
            this.feedDone(t, (q && (d.points||[]).length===0 && this.structSearchNodes().length===0) ? 'empty' : 'ready');
          }catch(e){
            if(this.feedAborted(e) || !this.feedLive(t)) return;
            this.feedFail(t, e.message);   // экран показывает «Поиск не ответил», остальное не валим
          }
        },

      structPeriodLabel(){
          const d=this._ovDefaultDates();
          return `Период: ${d.from} — ${d.to} (текущий месяц, как на Обзоре)`;
        },

      // П2: подписи типов — из общего словаря core.js (NODE_KINDS).
      structNodeKindLabel(kind){ return this.nodeKindLabel(kind); },

      structNodeName(nodeId){
          const n=(this.structNodes||[]).find(x=>x.id===nodeId);
          return n ? (n.name||n.code||('#'+nodeId)) : ('#'+nodeId);
        },

      structPointName(pointId){
          const p=(this.structPoints||[]).find(x=>x.point_id===pointId);
          return p ? (p.name||p.code||('#'+pointId)) : ('#'+pointId);
        },

      structNodeEdges(nodeId){
          return (this.structEdges||[]).filter(e=>e.from_node_id===nodeId || e.to_node_id===nodeId);
        },

      structFeed(pointId){
          // "Чем питается / что питает" (§8.3): связи, где ЭТА точка назначена
          // измерением (primary_point_id), и соседние сегменты по уже
          // опубликованной схеме сети (черновики сюда не попадают — они ещё не
          // прошли проверку леса, см. topology/publish).
          const measuredEdges = (this.structEdges||[]).filter(e=>e.primary_point_id===pointId).map(edge=>{
            // Партия 13, 13.3 (найдено автопроходом чек-листа, шаг 54): «Питается от» и «Питает»
            // называем УЗЛАМИ схемы («Ввод», «Тест-ветка»), а не линиями: у линии, заведённой
            // «Планом v3», нет имени, и на экран попадал служебный код вида pv3-e-1-2-1792…
            const fedBy = (this.structEdges||[]).filter(e2=>e2.to_node_id===edge.from_node_id)
              .map(e2=>({name: this.structNodeName(e2.from_node_id)}));
            const feeds = (this.structEdges||[]).filter(e2=>e2.from_node_id===edge.to_node_id)
              .map(e2=>({name: this.structNodeName(e2.to_node_id)}));
            return {edge, fedBy, feeds};
          });
          return {measuredEdges};
        },

      async submitPointDescription(){
          if(!this.structInspector || this.structInspector.kind!=='point') return;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/points/'+this.structInspector.id, {
              method:'PATCH', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({description: this.structInspector.data.description||'', expected_revision: rev})});
            const d = await r.json();
            if(!r.ok){
              this.showToast(this._apiErrorMessage(d, r.status), false);
              await this._loadPointInspector(this.structInspector.id);
              return;
            }
            await this._loadStructureLists();
            this._refreshStructHistoryIfOpen();   // карточку не перезагружаем, а запись в журнале уже есть
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
        },

      async simpleAddToGroup(groupId){
          if(!groupId || !this.structInspector) return;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/groups/'+groupId+'/members', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({point_id:this.structInspector.id, expected_revision:rev})});
            const d = await r.json();
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            this.structInspector.simpleGroupPick = '';
            await this._loadPointInspector(this.structInspector.id);
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
        },

      async simpleRemoveFromGroup(groupId){
          if(!this.structInspector) return;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/groups/'+groupId+'/members/'+this.structInspector.id+
              '?expected_revision='+rev, {method:'DELETE'});
            if(!r.ok){ const d = await r.json(); this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            await this._loadPointInspector(this.structInspector.id);
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
        },

      async openInspector(kind, id){
          if(id==null) return;
          if(this.tab!=='structurev2'){
            this.tab='structurev2';
            this._loadStructureTreeEnergy();
          }
          if(!this.structKnownDevicesLoaded) this._loadKnownDevices();
          if(!this.structInited){ this.structInited=true; await this._loadStructureLists(); }
          await this._loadInspectorData(kind, id);
        },

      async openInspectorInPlace(kind, id){
          if(id==null) return;
          if(!this.structInited){ this.structInited=true; await this._loadStructureLists(); }
          await this._loadInspectorData(kind, id);
        },

      async _loadInspectorData(kind, id){
          // История другой карточки не показывается: сбрасываем список, но
          // раскрытость блока оставляем — перебирая точки, пользователь видит
          // историю каждой без повторного клика (грузится в конце загрузчика).
          this.structHistory = {open:!!(this.structHistory&&this.structHistory.open),
            loading:false, error:null, items:[], next:null, startedAt:null, key:null};
          // Партия 13, 13.4 (A33): последняя запрошенная карточка побеждает — ответ
          // ранее выбранной не перетирает новую, её запросы отменяются (лента 'inspector').
          const t = this.feedStart('inspector');
          this._inspTok = t;
          this.feedReset('card-live');   // «устарело» прежней карточки к новой не относится
          this.structInspector = {kind, id, data:null};
          this.structInspectorLoading = true; this.structInspectorError = null;
          this.structInspectorPartial = null;
          try{
            if(kind==='point') await this._loadPointInspector(id);
            else if(kind==='group') await this._loadGroupInspector(id);
            else if(kind==='node') await this._loadNodeInspector(id);
            else if(kind==='edge') await this._loadEdgeInspector(id);
            else throw new Error('неизвестный тип объекта: '+kind);
            this.feedDone(t, this.structInspectorPartial ? 'partial' : 'ready');
            // Живые числа карточки узла/линии (лента 'card-live', опрос в общем тике): точка
            // отсчёта «обновлено» — момент загрузки карточки.
            if(this.feedLive(t) && this.structInspector && (kind==='node'||kind==='edge') &&
               !this.feeds['card-live']){
              const lt = this.feedStart('card-live', {live:true});
              this.feedDone(lt, 'ready');
            }
          }catch(e){
            if(this.feedAborted(e) || !this.feedLive(t)) return;
            this.structInspectorError = 'Не удалось загрузить карточку: '+e.message;
            this.feedFail(t, e.message);
          }finally{
            if(this.feedLive(t)) this.structInspectorLoading = false;
          }
        },

      closeInspector(){
          // Партия 13, 13.4: загрузка закрываемой карточки обесценивается — её запоздалый
          // ответ карточку заново не откроет.
          this.feedReset('inspector'); this.feedReset('card-live');
          this.structInspector=null; this.structInspectorError=null;
          this.structInspectorLoading=false; this.structInspectorPartial=null;
        },

      _structHistoryEntity(){
          const i=this.structInspector; if(!i) return null;
          const t={point:'metering_point', group:'group', node:'electrical_node',
                   edge:'electrical_edge'}[i.kind];
          return t ? {type:t, id:i.id} : null;
        },

      toggleStructHistory(){
          const h=this.structHistory;
          h.open=!h.open;
          if(h.open) return this.loadStructHistory(false);
        },

      // Открытая история перечитывается после перезагрузки карточки (правка
      // описания, смена прибора и т.п.) — на «Структуре»; на «Плане v3» тот же
      // инспектор без блока истории, лишних запросов нет.
      _refreshStructHistoryIfOpen(){
          if(this.tab==='structurev2' && this.structHistory && this.structHistory.open)
            return this.loadStructHistory(false);
        },

      async loadStructHistory(more){
          const ent=this._structHistoryEntity(); if(!ent) return;
          const h=this.structHistory;
          const key=ent.type+':'+ent.id;
          if(!more){ h.items=[]; h.next=null; }
          h.key=key; h.loading=true; h.error=null;
          try{
            let url='/api/v2/change-log?entity_type='+ent.type+'&entity_id='+ent.id+
                    '&related=1&limit=20';
            if(more && h.next!=null) url+='&before_id='+h.next;
            const r=await fetch(url);
            const d=await r.json();
            if(!r.ok) throw new Error(d.message||('HTTP '+r.status));
            if(h.key!==key) return;     // пока ждали, открыли другую карточку
            h.items = more ? h.items.concat(d.items||[]) : (d.items||[]);
            h.next = d.next_before_id==null ? null : d.next_before_id;
            h.startedAt = d.journal_started_at==null ? null : d.journal_started_at;
          }catch(e){
            if(h.key===key) h.error='Не удалось загрузить историю: '+e.message;
          }finally{
            if(h.key===key) h.loading=false;
          }
        },

      // Подпись записи журнала для человека: «изменено: линия», время, откуда.
      structHistoryTitle(it){
          const act={create:'создано', update:'изменено', delete:'удалено',
                     archive:'архивировано', restore:'восстановлено',
                     close:'закрыто', publish:'опубликовано'}[it.action]||it.action;
          const ent={metering_point:'точка', point_binding:'привязка прибора',
                     point_state:'состояние точки', meter_source:'источник данных',
                     point_location:'место точки', point_served_location:'обслуживаемое место',
                     location:'место', location_parent:'родитель места', group:'группа',
                     group_parent:'родитель группы', group_member:'состав группы',
                     electrical_node:'элемент схемы', electrical_edge:'линия',
                     balance_scope:'граница баланса', balance_member:'состав баланса',
                     plan:'план', plan_layout:'раскладка плана'}[it.entity_type]||it.entity_type;
          return ent+' #'+it.entity_id+' — '+act;
        },

      structHistoryWhen(it){
          const f=ts=>new Date(ts*1000).toLocaleString('ru-RU',
            {day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'});
          let s=f(it.recorded_at);
          if(it.effective_from!=null && it.effective_from!==it.recorded_at)
            s+=' · действует с '+f(it.effective_from);
          const src={web:'интерфейс', 'legacy-api':'старый API', wizard:'мастер переноса',
                     migration:'миграция', cli:'командная строка', system:'система'}[it.origin]||it.origin;
          return s+(src?' · '+src:'');
        },

      // Что именно поменялось: для update — «поле: было → стало» по полям, значения
      // которых различаются (служебные updated_at не показываем); для create/delete —
      // имя или код, если они есть в записи.
      structHistoryChanges(it){
          const skip={updated_at:1, created_at:1, id:1};
          const show=v=>v==null?'—':(typeof v==='object'?JSON.stringify(v):String(v));
          const o=it.old_value||{}, n=it.new_value||{};
          if(it.action==='update' || it.action==='archive' || it.action==='restore'
             || it.action==='close' || it.action==='publish'){
            const out=[];
            Object.keys(Object.assign({}, o, n)).forEach(k=>{
              if(skip[k]) return;
              if(JSON.stringify(o[k])!==JSON.stringify(n[k]))
                out.push(k+': '+show(o[k])+' → '+show(n[k]));
            });
            return out.slice(0,6);
          }
          const src=it.action==='delete'?o:n;
          const out=[];
          ['name','code','kind','state','point_id','group_id'].forEach(k=>{
            if(src[k]!=null) out.push(k+': '+show(src[k]));
          });
          return out;
        },

      async _loadPointInspector(id){
          const period=this._ovDefaultDates();
          const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
          const t=this._inspTok, sig={signal:t&&t.signal};
          const [pointR, snapR, bindingsR, groupsR] = await Promise.all([
            fetch('/api/v2/points/'+id, sig),
            fetch('/api/v2/snapshot?point_ids='+id, sig),
            fetch('/api/v2/points/'+id+'/bindings', sig),
            fetch('/api/v2/points/'+id+'/groups', sig),
          ]);
          if(!pointR.ok) throw new Error('точка не найдена (HTTP '+pointR.status+')');
          const point = await pointR.json();
          // location_path/meter_* берём из уже загруженного structure/points —
          // §8.3 требует, чтобы инспектор пользовался уже имеющимися ручками, а
          // не пересчитывал путь по местам заново на фронте.
          const structItem = (this.structPoints||[]).find(p=>p.point_id===id) || {};
          const data = Object.assign({}, point, {
            location_path: structItem.location_path,
            meter_device_id: structItem.meter_device_id,
            meter_controller_key: structItem.meter_controller_key,
            meter_serial: structItem.meter_serial,
          });
          const snap = snapR.ok ? (await snapR.json()).points : [];
          const bindings = bindingsR.ok ? (await bindingsR.json()) : [];
          const memberships = groupsR.ok ? (await groupsR.json()) : [];
          const groups = memberships.filter(m=>m.valid_to==null).map(m=>{
            const g=(this.structGroups||[]).find(x=>x.id===m.group_id);
            return {id: m.group_id, name: g?g.name:('#'+m.group_id)};
          });
          if(!this.feedLive(t)) return;
          this.structInspector = {
            kind:'point', id, data,
            snapshot: (snap&&snap[0]) || null,
            bindings: (bindings||[]).slice().sort((a,b)=>b.valid_from-a.valid_from),
            groups,
            consumption: null,
            // Состояние формы «добавить в группу», живёт вместе с карточкой
            // (сбрасывается при переходе на другую точку само собой).
            simpleGroupPick:'',
          };
          this._refreshStructHistoryIfOpen();
          // Расход за период — отдельным запросом, чтобы карточка сразу
          // показывала остальное, даже если metrics/query недоступен/медленный.
          try{
            const r = await fetch('/api/v2/metrics/query', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({mode:'measured', point_ids:[id], from:period.from, to:period.to, timezone:tz}),
              signal: t&&t.signal,
            });
            if(!this.feedLive(t)) return;
            if(r.ok) this.structInspector.consumption = await r.json();
            else this.structInspectorPartial = 'Расход за период не загрузился — остальное в карточке актуально.';
          }catch(e){
            if(this.feedAborted(e) || !this.feedLive(t)) return;
            this.structInspectorPartial = 'Расход за период не загрузился — остальное в карточке актуально.';
          }
        },

      async _loadGroupInspector(id){
          // §8.3: у группы вместо атрибутов прибора — состав и сумма/качество.
          const period=this._ovDefaultDates();
          const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
          const t=this._inspTok, sig={signal:t&&t.signal};
          const [groupR, membersR] = await Promise.all([
            fetch('/api/v2/groups/'+id, sig),
            fetch('/api/v2/groups/'+id+'/effective-members', sig),
          ]);
          if(!groupR.ok) throw new Error('группа не найдена (HTTP '+groupR.status+')');
          const data = await groupR.json();
          const membersResp = membersR.ok ? await membersR.json() : {points:[]};
          const members = membersResp.points || [];
          if(!this.feedLive(t)) return;
          this.structInspector = {kind:'group', id, data, members, sum:null};
          this._refreshStructHistoryIfOpen();
          if(members.length>0){
            try{
              const r = await fetch('/api/v2/metrics/query', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({mode:'sum', point_ids:members.map(m=>m.point_id), from:period.from, to:period.to, timezone:tz}),
                signal: t&&t.signal,
              });
              if(!this.feedLive(t)) return;
              if(r.ok) this.structInspector.sum = await r.json();
              else this.structInspectorPartial = 'Сумма по составу не загрузилась — остальное в карточке актуально.';
            }catch(e){
              if(this.feedAborted(e) || !this.feedLive(t)) return;
              this.structInspectorPartial = 'Сумма по составу не загрузилась — остальное в карточке актуально.';
            }
          }
        },

      async _loadNodeInspector(id){
          const t=this._inspTok, sig={signal:t&&t.signal};
          const r = await fetch('/api/v2/topology/nodes/'+id, sig);
          if(!r.ok) throw new Error('элемент схемы не найден (HTTP '+r.status+')');
          const data = await r.json();
          // «План v3», §1/§2: список линий узла (входящая+отходящие, каждая со
          // своей измеряющей точкой) и текстовое "где стоит" — берём имя места
          // из уже загруженного structLocations (общий список, не отдельный
          // запрос), чтобы карточка сразу показывала и то, и другое.
          let lines = [];
          try{
            const lr = await fetch('/api/v2/topology/nodes/'+id+'/lines', sig);
            if(lr.ok) lines = (await lr.json()).lines || [];
            else this.structInspectorPartial = 'Линии элемента не загрузились — список может быть неполным.';
          }catch(e){
            if(this.feedAborted(e) || !this.feedLive(t)) return;
            this.structInspectorPartial = 'Линии элемента не загрузились — список может быть неполным.';
          }
          if(!this.feedLive(t)) return;
          let locationText = '';
          if(data.location_id!=null){
            const loc = (this.structLocations||[]).find(l=>l.id===data.location_id);
            locationText = loc ? loc.name : '';
          }
          this.structInspector = {kind:'node', id, data, lines,
            nameDraft: data.name, locationTextDraft: locationText,
            kindDraft: data.kind,
            lineLive:{}, lineEnergy:{}, balance:null, balanceLoading:false, balanceError:null};
          this._refreshStructHistoryIfOpen();
          this.planV3NodeCardError = null;
          this.planV3AddConsumerError = null; this.planV3AddConsumerName = '';
          this.nodeMeterOpen = false;
          // Э3/B7: живые P-now/E-за-период по линиям и «Баланс щита». С партии 13
          // (П6) карточка элемента на «Структуре» — та же, что на «Плане v3»,
          // поэтому живые числа считаются и там.
          if(this.tab==='planv3') this.planV3XyLoad();
          if(this.tab==='planv3' || this.tab==='structurev2'){
            this._planV3RefreshNodeLiveNow();
            this._planV3LoadNodeLineEnergy();
            this._planV3LoadNodeBalance(id);
          }
        },

      async _loadEdgeInspector(id){
          const t=this._inspTok;
          const r = await fetch('/api/v2/topology/edges/'+id, {signal:t&&t.signal});
          if(!r.ok) throw new Error('связь не найдена (HTTP '+r.status+')');
          const data = await r.json();
          // Партия 7, Этап 3, Э3/B5: карточка линии «Плана v3» переиспользует
          // этот же loader (общий structInspector) — «Структура» показывает
          // его read-only (см. HTML ниже), «План v3» добавляет редактирование
          // и живые числа поверх тех же данных.
          const {currentA, loadPct, phases, power_w, failed}=await this._planEdgeLiveMetrics(data, t&&t.signal);
          if(!this.feedLive(t)) return;
          if(failed) this.structInspectorPartial = 'Ток и загрузка линии не получены — остальное в карточке актуально.';
          this.structInspector = {
            kind:'edge', id, data, currentA, loadPct, phases, power_w,
            nameDraft: data.name||'',
            ratedDraft: data.rated_current_a!=null ? String(data.rated_current_a) : '',
            cableDraft: data.cable_note||'',
          };
          this._refreshStructHistoryIfOpen();
          this.planV3EdgeCardError=null;
          this.planV3AttachEdgeId=null; this.planV3AttachDeviceId=''; this.planV3AttachError=null;
        },

      openCreatePanel(kind){
          this.structCreatePanel = kind;
          this.structCreateError = null;
          this.structCreateOk = null;
          if(kind==='publish') this._loadDraftEdges();
        },

      closeCreatePanel(){
          this.structCreatePanel = null; this.structCreateError = null; this.structCreateOk = null;
        },

      async _structCurrentRev(){
          const r = await fetch('/api/v2/revision');
          const d = await r.json();
          return d.configuration_revision;
        },

      async _loadKnownDevices(){
          // Тот же список приборов, что и классическая «Настройки» → «Добавить
          // счётчик» (registry/meters — уже зарегистрированные, meters/unregistered
          // — видны в MQTT, но ещё не в реестре) — партия 5 не заводит новый
          // способ узнать прибор, только переиспользует эти два уже существующих.
          try{
            const [rr, ur] = await Promise.all([
              fetch('/api/registry/meters'), fetch('/api/meters/unregistered'),
            ]);
            const rd = rr.ok ? await rr.json() : {items:[]};
            const ud = ur.ok ? await ur.json() : {items:[]};
            const seen = new Set();
            const list = [];
            (rd.items||[]).forEach(m=>{
              if(seen.has(m.device_id)) return;
              seen.add(m.device_id);
              const name=m.display_name||m.device_id;
              const emu=!!m.is_emulator || this.emuIs(m.device_id);
              list.push({device_id:m.device_id, name, label:this._emuLabel(name, emu),
                         phase_channels:!!m.phase_channels, is_emulator:emu});
            });
            (ud.items||[]).forEach(m=>{
              if(seen.has(m.device_id)) return;
              seen.add(m.device_id);
              const name=m.mqtt_name||m.device_id;
              const emu=!!m.is_emulator || this.emuIs(m.device_id);
              list.push({device_id:m.device_id, name, label:this._emuLabel(name, emu)+' — виден в MQTT',
                         phase_channels:!!m.phase_channels, is_emulator:emu});
            });
            this.structKnownDevices = list;
          }catch(e){ this.structKnownDevices = []; }
          this.structKnownDevicesLoaded = true;
        },

      async _loadDraftEdges(){
          try{
            const r = await fetch('/api/v2/topology/edges'); // по умолчанию state=draft
            this.structDraftEdges = r.ok ? (await r.json()) : [];
            this.publishSelected = this.structDraftEdges.map(e=>e.id);
            this.publishValidation = null;
          }catch(e){ this.structDraftEdges = []; }
        },

      async structSubmitEdge(){
          if(!this.edgeForm.from_node_id || !this.edgeForm.to_node_id){
            this.structCreateError = 'Выберите оба узла связи'; return;
          }
          if(this.edgeForm.from_node_id === this.edgeForm.to_node_id){
            this.structCreateError = 'Связь не может соединять узел сам с собой'; return;
          }
          this.structCreateSaving=true; this.structCreateError=null; this.structCreateOk=null;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/topology/edges', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({
                from_node_id:this.edgeForm.from_node_id, to_node_id:this.edgeForm.to_node_id,
                primary_point_id:this.edgeForm.primary_point_id||null,
                rated_current_a:this.edgeForm.rated_current_a?Number(this.edgeForm.rated_current_a):null,
                cable_note:this.edgeForm.cable_note||null, name:this.edgeForm.name||null,
                expected_revision:rev,
              })});
            const d = await r.json();
            if(!r.ok) throw new Error(this._apiErrorMessage(d, r.status));
            this.structCreateOk = 'Черновик связи создан — она появится в схеме только '+
              'после публикации (см. вкладку «Публикация» ниже).';
            this.edgeForm = {from_node_id:null, to_node_id:null, primary_point_id:null,
                              rated_current_a:'', cable_note:'', name:''};
            await this._loadDraftEdges();
          }catch(e){ this.structCreateError = e.message; }
          finally{ this.structCreateSaving=false; }
        },

      async structValidatePublish(){
          this.structCreateError = null;
          try{
            const r = await fetch('/api/v2/topology/validate', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({edge_ids:this.publishSelected})});
            this.publishValidation = await r.json();
          }catch(e){ this.structCreateError = e.message; }
        },

      async structPublish(){
          if(!this.publishSelected.length){ this.structCreateError='Нечего публиковать — нет выбранных черновиков'; return; }
          this.structCreateSaving=true; this.structCreateError=null; this.structCreateOk=null;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/topology/publish', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({edge_ids:this.publishSelected,
                                     expected_configuration_revision:rev})});
            const d = await r.json();
            if(!r.ok){
              if(d.code==='topology_conflict'){
                this.publishValidation = {ok:false, violations:d.path||[]};
                this.structCreateError = 'Публикация отклонена: связи образуют конфликт '+
                  '(см. нарушения ниже), схема не изменена. '+(d.message||'');
              }else{
                this.structCreateError = this._apiErrorMessage(d, r.status);
              }
              return;
            }
            this.structCreateOk = 'Опубликовано связей: '+d.edges.length+'.';
            this.publishValidation = null;
            await this._loadDraftEdges();
            await this._loadStructureLists();
          }catch(e){ this.structCreateError = e.message; }
          finally{ this.structCreateSaving=false; }
        },

      // «Показать на схеме»: открыть на «Плане v3» линию, которую измеряет
      // точка (карточка линии с живыми числами).
      async showPointOnScheme(pointId){
          const e=(this.structEdges||[]).find(x=>x.primary_point_id===pointId);
          if(!e){ this.showToast('Точка не стоит ни на одной линии схемы', false); return; }
          this.tab='planv3';
          await this.openPlanV3Tab();
          await this.openInspectorInPlace('edge', e.id);
        },

      openReplaceMeterForm(){
          if(!this.structKnownDevicesLoaded) this._loadKnownDevices();
          this.replaceMeterForm = {
            mode:'existing_device', device_id:'', display_name:'',
            channel_profile: (this.structInspector.snapshot && this.structInspector.snapshot.channel_profile) || 'total_3p',
            replacement_note:'', saving:false, error:null,
          };
        },

      closeReplaceMeterForm(){ this.replaceMeterForm = null; },

      async submitReplaceMeter(){
          const f = this.replaceMeterForm;
          if(!f.device_id){ f.error = 'Выберите прибор'; return; }
          f.saving = true; f.error = null;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/points/'+this.structInspector.id+'/replace-meter', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({
                new_meter:{device_id:f.device_id, display_name:f.display_name||null},
                channel_profile:f.channel_profile,
                replacement_note:f.replacement_note||null,
                expected_revision:rev,
              })});
            const d = await r.json();
            if(!r.ok){ f.error = this._apiErrorMessage(d, r.status); return; }
            this.replaceMeterForm = null;
            this.showToast('Прибор заменён. Старая привязка закрыта в момент замены — история сохранена.');
            await this._loadPointInspector(this.structInspector.id);
            await this._loadStructureLists();
          }catch(e){ f.error = e.message; }
          finally{ f.saving = false; }
        },

      openInspectorForPlanItem(item){
          const t=this.planV2ItemInspectorTarget(item);
          if(t) this.openInspector(t.kind, t.id);
        }
    };
  });
})();

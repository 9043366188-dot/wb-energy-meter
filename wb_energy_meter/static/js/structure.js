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

      structSelectedLocationId:null,

      locEditForm:null,

      structInspector:null,

      structInspectorLoading:false,

      structInspectorError:null,

      _structSearchSeq:0,

      structCreatePanel:null,

      structCreateSaving:false,

      structCreateError:null,

      structCreateOk:null,

      locForm:{name:'', kind:'room', parent_id:null},

      pointForm:{code:'', name:'', installation_location_id:null, bind:true,
                   device_id:'', display_name:'', channel_profile:'total_3p'},

      nodeForm:{code:'', name:'', kind:'panel', location_id:null},

      edgeForm:{from_node_id:null, to_node_id:null, primary_point_id:null,
                  rated_current_a:'', cable_note:'', name:''},

      structKnownDevices:[],

      structKnownDevicesLoaded:false,

      structDraftEdges:[],

      publishSelected:[],

      publishValidation:null,

      replaceMeterForm:null,

      editMembershipForm:null,

      async openStructureTab(){
          if(this.structInited) return;
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
            this.structPoints = (await pr.json()).points || [];
            this.structLocations = lr.ok ? await lr.json() : [];
            this.structGroups = gr.ok ? await gr.json() : [];
            this.structNodes = nr.ok ? await nr.json() : [];
            this.structEdges = er.ok ? await er.json() : [];
            await this._loadStructureSnapshot();
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
          try{
            const r=await fetch('/api/v2/snapshot');
            const d=await r.json();
            this._structSnapshotById = {};
            (d.points||[]).forEach(p=>{ this._structSnapshotById[p.point_id]=p; });
          }catch(e){ this._structSnapshotById = this._structSnapshotById||{}; }
        },

      structSnapshotFor(pointId){
          return (this._structSnapshotById||{})[pointId] || null;
        },

      async structureSearch(){
          // A02: поиск по имени/коду/MQTT ID/серийнику/пути через
          // /api/v2/structure/points?q= — сервер уже фильтрует и не прячет
          // непривязанные/неразмещённые точки. _structSearchSeq защищает от
          // устаревшего ответа, если пользователь печатает быстро.
          const seq = ++this._structSearchSeq;
          const q = this.structQuery.trim();
          try{
            const r = await fetch('/api/v2/structure/points'+(q?('?q='+encodeURIComponent(q)):''));
            const d = await r.json();
            if(seq !== this._structSearchSeq) return;
            if(q){
              this._structSearchResults = d.points||[];
            }else{
              this.structPoints = d.points||[];
              this._structSearchResults = null;
              await this._loadStructureSnapshot();
            }
          }catch(e){ /* поиск best-effort, не валим экран */ }
        },

      openLocationEdit(locId){
          const loc = (this.structLocations||[]).find(l=>l.id===locId);
          if(!loc) return;
          this.locEditForm = {id: locId, name: loc.name, code: loc.code||'', saving:false, error:null};
        },

      async saveLocationEdit(){
          const f = this.locEditForm;
          if(!f) return;
          f.saving = true; f.error = null;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/locations/'+f.id, {
              method:'PATCH', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({name: f.name, code: f.code||null, expected_revision: rev})});
            const d = await r.json();
            if(!r.ok){ f.error = this._apiErrorMessage(d, r.status); return; }
            this.showToast('Место обновлено', true);
            await this._loadStructureLists();
          }catch(e){ f.error = e.message; }
          finally{ f.saving = false; }
        },

      async archiveSelectedLocation(){
          const f = this.locEditForm;
          if(!f) return;
          f.saving = true; f.error = null;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/locations/'+f.id, {
              method:'PATCH', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({archived:true, expected_revision: rev})});
            const d = await r.json();
            if(!r.ok){ f.error = this._apiErrorMessage(d, r.status); return; }
            this.showToast('Место архивировано', true);
            this.structSelectedLocationId = null;
            this.locEditForm = null;
            await this._loadStructureLists();
          }catch(e){ f.error = e.message; }
          finally{ if(this.locEditForm) this.locEditForm.saving = false; }
        },

      structLocationRows(){
          // Плоский отсортированный список мест с отступом по глубине —
          // визуально дерево, без рекурсивных Alpine-компонентов (тот же приём,
          // что и остальной интерфейс — минимум магии в разметке).
          const byParent = {};
          (this.structLocations||[]).filter(l=>!l.archived_at).forEach(l=>{
            const key = l.parent_id==null ? 'root' : String(l.parent_id);
            (byParent[key] = byParent[key]||[]).push(l);
          });
          const countsByLoc = this._structPointCountsByLocation();
          const rows = [];
          const walk = (parentKey, depth) => {
            const children = (byParent[parentKey]||[]).slice()
              .sort((a,b)=>(a.sort_order-b.sort_order)||String(a.name).localeCompare(String(b.name)));
            children.forEach(l=>{
              rows.push({id:l.id, name:l.name, depth, pointCount: countsByLoc[l.id]||0});
              walk(String(l.id), depth+1);
            });
          };
          walk('root', 0);
          return rows;
        },

      _structPointCountsByLocation(){
          const counts = {};
          (this.structPoints||[]).forEach(p=>{
            if(p.location_id!=null) counts[p.location_id]=(counts[p.location_id]||0)+1;
          });
          return counts;
        },

      structUnplacedPoints(){
          // §8.3: "непривязанные/неразмещённые точки — в отдельной доступной
          // коллекции, никогда не пропадают молча" — этот бакет и есть та
          // коллекция для мест (по прибору — badge "не привязана" в списке).
          return (this.structPoints||[]).filter(p=>p.location_id==null);
        },

      structCenterTitle(){
          if(this.structQuery.trim()!=='') return `Результаты поиска «${this.structQuery.trim()}»`;
          if(this.structSelectedLocationId==='unplaced') return 'Точки без места установки';
          if(this.structSelectedLocationId==null) return 'Выберите место слева';
          const loc = (this.structLocations||[]).find(l=>l.id===this.structSelectedLocationId);
          return loc ? ('Место: '+loc.name) : '';
        },

      structCenterPoints(){
          if(this.structQuery.trim()!=='') return this._structSearchResults || [];
          if(this.structSelectedLocationId==='unplaced') return this.structUnplacedPoints();
          if(this.structSelectedLocationId==null) return [];
          return (this.structPoints||[]).filter(p=>p.location_id===this.structSelectedLocationId);
        },

      structPeriodLabel(){
          const d=this._ovDefaultDates();
          return `Период: ${d.from} — ${d.to} (текущий месяц, как на Обзоре)`;
        },

      structNodeKindLabel(kind){
          return {source:'Ввод', panel:'Щит', junction:'Узел', load:'Нагрузка'}[kind]||kind;
        },

      structNodeName(nodeId){
          const n=(this.structNodes||[]).find(x=>x.id===nodeId);
          return n ? (n.name||n.code||('#'+nodeId)) : ('#'+nodeId);
        },

      structMissingInfo(p){
          const missing=[];
          if(!p.bound) missing.push('нет прибора');
          if(!p.location_id) missing.push('нет места');
          if(p.measured_edge_id==null) missing.push('не стоит на линии');
          if(!p.placed_on_plan) missing.push('не на плане');
          return missing;
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
            const fedBy = (this.structEdges||[]).filter(e2=>e2.to_node_id===edge.from_node_id)
              .map(e2=>({name: e2.name, code: e2.code}));
            const feeds = (this.structEdges||[]).filter(e2=>e2.from_node_id===edge.to_node_id)
              .map(e2=>({name: e2.name, code: e2.code}));
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
          if(this.tab!=='structurev2'){ this.tab='structurev2'; }
          if(!this.structInited){ this.structInited=true; await this._loadStructureLists(); }
          await this._loadInspectorData(kind, id);
        },

      async openInspectorInPlace(kind, id){
          if(id==null) return;
          if(!this.structInited){ this.structInited=true; await this._loadStructureLists(); }
          await this._loadInspectorData(kind, id);
        },

      async _loadInspectorData(kind, id){
          this.structInspector = {kind, id, data:null};
          this.structInspectorLoading = true; this.structInspectorError = null;
          try{
            if(kind==='point') await this._loadPointInspector(id);
            else if(kind==='group') await this._loadGroupInspector(id);
            else if(kind==='node') await this._loadNodeInspector(id);
            else if(kind==='edge') await this._loadEdgeInspector(id);
            else throw new Error('неизвестный тип объекта: '+kind);
          }catch(e){
            this.structInspectorError = 'Не удалось загрузить карточку: '+e.message;
          }finally{
            this.structInspectorLoading = false;
          }
        },

      closeInspector(){ this.structInspector=null; this.structInspectorError=null; },

      async _loadPointInspector(id){
          const period=this._ovDefaultDates();
          const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
          const [pointR, snapR, bindingsR, groupsR] = await Promise.all([
            fetch('/api/v2/points/'+id),
            fetch('/api/v2/snapshot?point_ids='+id),
            fetch('/api/v2/points/'+id+'/bindings'),
            fetch('/api/v2/points/'+id+'/groups'),
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
          // Расход за период — отдельным запросом, чтобы карточка сразу
          // показывала остальное, даже если metrics/query недоступен/медленный.
          try{
            const r = await fetch('/api/v2/metrics/query', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({mode:'measured', point_ids:[id], from:period.from, to:period.to, timezone:tz}),
            });
            if(r.ok) this.structInspector.consumption = await r.json();
          }catch(e){ /* не критично для остальной карточки */ }
        },

      async _loadGroupInspector(id){
          // §8.3: у группы вместо атрибутов прибора — состав и сумма/качество.
          const period=this._ovDefaultDates();
          const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
          const [groupR, membersR] = await Promise.all([
            fetch('/api/v2/groups/'+id),
            fetch('/api/v2/groups/'+id+'/effective-members'),
          ]);
          if(!groupR.ok) throw new Error('группа не найдена (HTTP '+groupR.status+')');
          const data = await groupR.json();
          const membersResp = membersR.ok ? await membersR.json() : {points:[]};
          const members = membersResp.points || [];
          this.structInspector = {kind:'group', id, data, members, sum:null};
          if(members.length>0){
            try{
              const r = await fetch('/api/v2/metrics/query', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({mode:'sum', point_ids:members.map(m=>m.point_id), from:period.from, to:period.to, timezone:tz}),
              });
              if(r.ok) this.structInspector.sum = await r.json();
            }catch(e){ /* сумма необязательна для остальной карточки */ }
          }
        },

      async _loadNodeInspector(id){
          const r = await fetch('/api/v2/topology/nodes/'+id);
          if(!r.ok) throw new Error('узел не найден (HTTP '+r.status+')');
          const data = await r.json();
          // «План v3», §1/§2: список линий узла (входящая+отходящие, каждая со
          // своей измеряющей точкой) и текстовое "где стоит" — берём имя места
          // из уже загруженного structLocations (общий список, не отдельный
          // запрос), чтобы карточка сразу показывала и то, и другое.
          let lines = [];
          try{
            const lr = await fetch('/api/v2/topology/nodes/'+id+'/lines');
            if(lr.ok) lines = (await lr.json()).lines || [];
          }catch(e){ /* необязательно для остальной карточки */ }
          let locationText = '';
          if(data.location_id!=null){
            const loc = (this.structLocations||[]).find(l=>l.id===data.location_id);
            locationText = loc ? loc.name : '';
          }
          this.structInspector = {kind:'node', id, data, lines,
            nameDraft: data.name, locationTextDraft: locationText,
            kindDraft: data.kind,
            lineLive:{}, lineEnergy:{}, balance:null, balanceLoading:false, balanceError:null};
          this.planV3NodeCardError = null;
          this.planV3AddConsumerError = null; this.planV3AddConsumerName = '';
          // Э3/B7: живые P-now/E-за-период по линиям и «Баланс щита» — только
          // на «Плане v3» (та же карточка на «Структуре» — read-only снимок
          // конфигурации, без живых запросов на каждый клик по дереву).
          if(this.tab==='planv3'){
            this._planV3RefreshNodeLiveNow();
            this._planV3LoadNodeLineEnergy();
            this._planV3LoadNodeBalance(id);
          }
        },

      async _loadEdgeInspector(id){
          const r = await fetch('/api/v2/topology/edges/'+id);
          if(!r.ok) throw new Error('связь не найдена (HTTP '+r.status+')');
          const data = await r.json();
          // Партия 7, Этап 3, Э3/B5: карточка линии «Плана v3» переиспользует
          // этот же loader (общий structInspector) — «Структура» показывает
          // его read-only (см. HTML ниже), «План v3» добавляет редактирование
          // и живые числа поверх тех же данных.
          const {currentA, loadPct, phases, power_w}=await this._planEdgeLiveMetrics(data);
          this.structInspector = {
            kind:'edge', id, data, currentA, loadPct, phases, power_w,
            nameDraft: data.name||'',
            ratedDraft: data.rated_current_a!=null ? String(data.rated_current_a) : '',
            cableDraft: data.cable_note||'',
          };
          this.planV3EdgeCardError=null;
          this.planV3AttachEdgeId=null; this.planV3AttachDeviceId=''; this.planV3AttachError=null;
        },

      openCreatePanel(kind){
          this.structCreatePanel = kind;
          this.structCreateError = null;
          this.structCreateOk = null;
          if(kind==='point' && !this.structKnownDevicesLoaded) this._loadKnownDevices();
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
              list.push({device_id:m.device_id, label:(m.display_name||m.device_id)+' — уже в реестре'});
            });
            (ud.items||[]).forEach(m=>{
              if(seen.has(m.device_id)) return;
              seen.add(m.device_id);
              list.push({device_id:m.device_id, label:(m.mqtt_name||m.device_id)+' — виден в MQTT, не в реестре'});
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

      async structSubmitLocation(){
          if(!this.locForm.name.trim()){ this.structCreateError='Введите название места'; return; }
          this.structCreateSaving=true; this.structCreateError=null; this.structCreateOk=null;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/locations', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({
                name:this.locForm.name.trim(), kind:this.locForm.kind,
                parent_id:this.locForm.parent_id||null, expected_revision:rev,
              })});
            const d = await r.json();
            if(!r.ok) throw new Error(this._apiErrorMessage(d, r.status));
            this.structCreateOk = 'Место «'+d.name+'» создано.';
            this.locForm = {name:'', kind:'room', parent_id:null};
            await this._loadStructureLists();
          }catch(e){ this.structCreateError = e.message; }
          finally{ this.structCreateSaving=false; }
        },

      async structSubmitPoint(){
          if(!this.pointForm.code.trim() || !this.pointForm.name.trim()){
            this.structCreateError = 'Код и имя точки обязательны'; return;
          }
          if(this.pointForm.bind && !this.pointForm.device_id){
            this.structCreateError = 'Выберите прибор из списка либо снимите галку привязки';
            return;
          }
          this.structCreateSaving=true; this.structCreateError=null; this.structCreateOk=null;
          try{
            let rev = await this._structCurrentRev();
            let r = await fetch('/api/v2/points', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({
                code:this.pointForm.code.trim(), name:this.pointForm.name.trim(),
                installation_location_id:this.pointForm.installation_location_id||null,
                expected_revision:rev,
              })});
            let d = await r.json();
            if(!r.ok) throw new Error(this._apiErrorMessage(d, r.status));
            const pointName = d.name;
            let bound = false;
            if(this.pointForm.bind && this.pointForm.device_id){
              rev = await this._structCurrentRev();
              const br = await fetch('/api/v2/points/'+d.id+'/bindings', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({
                  new_meter:{device_id:this.pointForm.device_id,
                             display_name:this.pointForm.display_name||null},
                  channel_profile:this.pointForm.channel_profile,
                  expected_revision:rev,
                })});
              const bd = await br.json();
              if(!br.ok){
                // Точка уже создана — сообщаем честно, что осталось привязать
                // отдельно (через «заменить прибор» в карточке), а не теряем
                // это молча.
                this.structCreateOk = 'Точка «'+pointName+'» создана, но привязка к '+
                  'прибору не удалась: '+this._apiErrorMessage(bd, br.status)+
                  ' Повторите привязку из карточки точки.';
                await this._loadStructureLists();
                this.pointForm = {code:'', name:'', installation_location_id:null,
                                   bind:true, device_id:'', display_name:'', channel_profile:'total_3p'};
                return;
              }
              bound = true;
            }
            this.structCreateOk = 'Точка «'+pointName+'» создана'+(bound?' и привязана к прибору.':'.');
            this.pointForm = {code:'', name:'', installation_location_id:null,
                               bind:true, device_id:'', display_name:'', channel_profile:'total_3p'};
            await this._loadStructureLists();
          }catch(e){ this.structCreateError = e.message; }
          finally{ this.structCreateSaving=false; }
        },

      async structSubmitNode(){
          if(!this.nodeForm.code.trim() || !this.nodeForm.name.trim()){
            this.structCreateError = 'Код и имя узла обязательны'; return;
          }
          this.structCreateSaving=true; this.structCreateError=null; this.structCreateOk=null;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/topology/nodes', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({
                code:this.nodeForm.code.trim(), name:this.nodeForm.name.trim(),
                kind:this.nodeForm.kind, location_id:this.nodeForm.location_id||null,
                expected_revision:rev,
              })});
            const d = await r.json();
            if(!r.ok) throw new Error(this._apiErrorMessage(d, r.status));
            this.structCreateOk = 'Узел «'+d.name+'» ('+this.structNodeKindLabel(d.kind)+') создан.';
            this.nodeForm = {code:'', name:'', kind:'panel', location_id:null};
            await this._loadStructureLists();
          }catch(e){ this.structCreateError = e.message; }
          finally{ this.structCreateSaving=false; }
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

      async showPointOnPlan(pointId){
          this.structCreateError = null; this.planPickerChoices = null;
          try{
            const pr = await fetch('/api/v2/plans');
            const plans = pr.ok ? await pr.json() : [];
            const matches = [];
            for(const p of (Array.isArray(plans)?plans:[])){
              const ir = await fetch('/api/v2/plans/'+p.id+'/items');
              if(!ir.ok) continue;
              const items = await ir.json();
              (Array.isArray(items)?items:[]).forEach(it=>{
                if(it.kind==='point' && it.point_id===pointId) matches.push({plan:p, item:it});
              });
            }
            if(matches.length===0){
              this.showToast('Точка не размещена ни на одном плане', false);
              return;
            }
            if(matches.length===1){
              await this._planV2GoToItem(matches[0].plan.id, matches[0].item.id);
              return;
            }
            this.planPickerChoices = matches;
          }catch(e){ this.showToast('Ошибка: '+e.message, false); }
        },

      async goToPlanPickerChoice(choice){
          this.planPickerChoices = null;
          await this._planV2GoToItem(choice.plan.id, choice.item.id);
        },

      openReplaceMeterForm(){
          if(!this.structKnownDevicesLoaded) this._loadKnownDevices();
          this.replaceMeterForm = {
            mode:'existing_device', device_id:'', display_name:'',
            channel_profile: (this.structInspector.snapshot && this.structInspector.snapshot.channel_profile) || 'total_3p',
            replacement_note:'', saving:false, error:null,
          };
          this.editMembershipForm = null;
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

      openEditMembershipForm(){
          const cur = this.structInspector.data;
          this.editMembershipForm = {
            installation_location_id: cur.installation_location_id,
            add_group_id: '', saving:false, error:null,
          };
          this.replaceMeterForm = null;
        },

      closeEditMembershipForm(){ this.editMembershipForm = null; },

      async submitEditLocation(){
          const f = this.editMembershipForm;
          f.saving = true; f.error = null;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/points/'+this.structInspector.id, {
              method:'PATCH', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({
                installation_location_id: f.installation_location_id||null,
                expected_revision: rev,
              })});
            const d = await r.json();
            if(!r.ok){ f.error = this._apiErrorMessage(d, r.status); return; }
            this.showToast('Место установки обновлено');
            await this._loadPointInspector(this.structInspector.id);
            await this._loadStructureLists();
          }catch(e){ f.error = e.message; }
          finally{ f.saving = false; }
        },

      async addPointToGroup(){
          const f = this.editMembershipForm;
          if(!f.add_group_id){ f.error = 'Выберите группу'; return; }
          f.saving = true; f.error = null;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/groups/'+f.add_group_id+'/members', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({point_id:this.structInspector.id, expected_revision:rev})});
            const d = await r.json();
            if(!r.ok){ f.error = this._apiErrorMessage(d, r.status); return; }
            f.add_group_id = '';
            await this._loadPointInspector(this.structInspector.id);
          }catch(e){ f.error = e.message; }
          finally{ f.saving = false; }
        },

      async removePointFromGroup(groupId){
          const f = this.editMembershipForm;
          f.saving = true; f.error = null;
          try{
            const rev = await this._structCurrentRev();
            const r = await fetch('/api/v2/groups/'+groupId+'/members/'+this.structInspector.id+
              '?expected_revision='+rev, {method:'DELETE'});
            if(!r.ok){ const d = await r.json(); f.error = this._apiErrorMessage(d, r.status); return; }
            await this._loadPointInspector(this.structInspector.id);
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

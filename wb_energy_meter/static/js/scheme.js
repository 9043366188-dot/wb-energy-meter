// scheme.js — партия 13 (находки пилота П1, П2, П4, П5, П6): всё, что общее у
// «Плана v3» и «Структуры» как у двух видов одной схемы.
//   * свободные/занятые приборы в списках выбора (П4);
//   * «+ Счётчик» в карточке элемента схемы — одной серверной операцией
//     POST /api/v2/topology/nodes/<id>/meters (П5);
//   * «Убрать счётчик», «Архивировать точку», «Убрать с плана», «Архивировать
//     узел», «Вывести линию из работы», «Удалить план» — с подтверждением
//     на странице (не window.confirm) и понятным текстом отказа (П1);
//   * дерево «Структуры» по опубликованной схеме и раздел «Не на схеме» (П6).
// Серверные ручки и их коды отказов описаны в api_v2.py; здесь только вызовы.
(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function schemePart() {
    return {
      // ---- подтверждение действия (одна модалка на всё приложение)
      // Всегда объект (шаблон читает поля и в момент закрытия); открыто ли окно —
      // флаг uiConfirmOpen.
      uiConfirmOpen:false,
      uiConfirm:{kind:'', payload:{}, title:'', text:'', lines:[], okLabel:'', extraLabel:null,
                 extraVariant:null, defaultVariant:null, busy:false, error:null},

      // ---- «+ Счётчик» в карточке узла
      // Форма — всегда объект (шаблоны x-model читают её поля даже в момент
      // закрытия); открыта ли она, говорит флаг nodeMeterOpen.
      nodeMeterOpen:false,
      nodeMeterForm:{node_id:null, device_id:'', name:'', nameTouched:false, measures:'input',
                     out_target:'', new_load_name:'', fed_from_node_id:'', saving:false, error:null},

      // ---- П4: приборы, занятые точками (с сервера, structure/points)
      structOccupiedDevices:[],

      // ---- П1: «Показать архивные»
      structShowArchived:false,
      structArchivedPoints:[],

      // ---- П6: энергия за период по точкам для дерева (reports/query)
      structTreeEnergy:{},

      // «Приборы без точки учёта» → «поставить на…»: выбранный элемент схемы
      structDeviceTarget:{},

      structNewNodeOpen:false,
      structNewNode:{kind:'panel', name:'', saving:false, error:null},

      // ================================================================
      // П4. Свободные и занятые приборы
      // ================================================================
      structOccupiedMap(){
          const m={};
          (this.structOccupiedDevices||[]).forEach(o=>{ m[o.device_id]=o; });
          return m;
        },

      // Список для <select>: сперва свободные, потом занятые (серые,
      // disabled) с именем точки-владельца. Занятый прибор выбрать нельзя —
      // сервер всё равно ответил бы 409.
      structDeviceOptions(){
          const occ=this.structOccupiedMap();
          const free=[], busy=[];
          (this.structKnownDevices||[]).forEach(d=>{
            const o=occ[d.device_id];
            if(o){
              busy.push({device_id:d.device_id, disabled:true,
                label:(d.name||d.device_id)+' — занят точкой «'+(o.point_name||'без имени')+'»'});
            }else{
              free.push({device_id:d.device_id, disabled:false, label:d.label});
            }
          });
          return free.concat(busy);
        },

      structFreeDevices(){
          const occ=this.structOccupiedMap();
          return (this.structKnownDevices||[]).filter(d=>!occ[d.device_id]);
        },

      structDeviceName(deviceId){
          const d=(this.structKnownDevices||[]).find(x=>x.device_id===deviceId);
          return d ? (d.name||deviceId) : deviceId;
        },

      // ================================================================
      // Что стоит на схеме: счётчики элемента, дерево, «Не на схеме»
      // ================================================================
      // Счётчики элемента — точки на действующих линиях, входящих в него и
      // выходящих из него. Точка по-прежнему измеряет ЛИНИЮ; здесь только
      // подпись роли на языке пользователя.
      structNodeMeters(nodeId){
          const node=(this.structNodes||[]).find(n=>n.id===nodeId);
          if(!node) return [];
          const out=[];
          (this.structEdges||[]).forEach(e=>{
            if(e.primary_point_id==null) return;
            let role=null, direction=null;
            if(e.to_node_id===nodeId){
              direction='in';
              role = node.kind==='panel' ? 'ввод щита'
                   : (node.kind==='load' ? 'вход нагрузки' : 'вход ответвления');
            }else if(e.from_node_id===nodeId){
              direction='out';
              role = 'линия к «'+this.structNodeName(e.to_node_id)+'»';
            }
            if(role==null) return;
            const p=(this.structPoints||[]).find(x=>x.point_id===e.primary_point_id);
            const snap=this.structSnapshotFor(e.primary_point_id);
            out.push({
              point_id:e.primary_point_id, edge_id:e.id, role, direction,
              name: p ? (p.name||p.code) : ('#'+e.primary_point_id),
              device_id: p ? p.meter_device_id : null,
              power_w: snap ? snap.power_w : null,
              energy: (this.structTreeEnergy||{})[e.primary_point_id]||null,
            });
          });
          out.sort((a,b)=>(a.direction==='in'?0:1)-(b.direction==='in'?0:1)
                          || String(a.role).localeCompare(String(b.role)));
          return out;
        },

      structNodeMeterCount(nodeId){ return this.structNodeMeters(nodeId).length; },

      // В дереве «Структуры» счётчик стоит один раз — под элементом, который
      // линия питает (ввод щита, вход нагрузки): так одна точка не выглядит
      // двумя. В карточке элемента (structNodeMeters) видны и входящие, и исходящие.
      structTreeMeters(nodeId){
          return this.structNodeMeters(nodeId).filter(m=>m.direction==='in');
        },

      // Мощность счётчика: живое значение карточки (обновляется каждые 5 с),
      // иначе — из снимка дерева.
      structMeterPower(m){
          const live=((this.structInspector&&this.structInspector.lineLive)||{})[m.point_id];
          if(live && live.power_w!=null) return live.power_w;
          return m.power_w;
        },

      structMeterLiveText(m){
          const pw=this.structMeterPower(m);
          const parts=[];
          if(pw!=null) parts.push((pw/1000).toFixed(2)+' кВт');
          if(m.energy) parts.push('за период: '+this.structEnergyText(m.energy));
          return parts.join(' · ');
        },

      // Подпись счётчика для списка: «имя · прибор — роль».
      structMeterTitle(m){
          const dev = m.device_id && m.device_id!==m.name ? (' · '+m.device_id) : '';
          return m.name+dev+' — '+m.role;
        },

      // Дерево по опубликованным действующим линиям: корни — элементы без
      // входящей линии (вводы, а также ещё не подключённые элементы),
      // дальше по линиям вниз. Плоский список строк с глубиной — тот же
      // приём, что у прежнего дерева мест (без рекурсивных компонентов).
      // Это ТА ЖЕ схема, что рисует «План v3»: те же узлы и те же линии
      // (structNodes/structEdges), другой вид.
      structTreeRows(){
          const nodes=(this.structNodes||[]).filter(n=>!n.archived_at);
          const byId=new Map(nodes.map(n=>[n.id,n]));
          const hasIncoming=new Set();
          const outgoing=new Map();
          (this.structEdges||[]).forEach(e=>{
            if(!byId.has(e.from_node_id) || !byId.has(e.to_node_id)) return;
            hasIncoming.add(e.to_node_id);
            if(!outgoing.has(e.from_node_id)) outgoing.set(e.from_node_id, []);
            outgoing.get(e.from_node_id).push(e);
          });
          const order={source:0, panel:1, junction:2, load:3};
          const cmp=(a,b)=>((order[a.kind]||0)-(order[b.kind]||0))
                           || String(a.name||'').localeCompare(String(b.name||''));
          const rows=[];
          const seen=new Set();
          const walk=(n, depth)=>{
            if(seen.has(n.id)) return;
            seen.add(n.id);
            rows.push({node:n, depth});
            (outgoing.get(n.id)||[])
              .map(e=>byId.get(e.to_node_id)).sort(cmp)
              .forEach(ch=>walk(ch, depth+1));
          };
          nodes.filter(n=>!hasIncoming.has(n.id)).sort(cmp).forEach(n=>walk(n,0));
          nodes.slice().sort(cmp).forEach(n=>walk(n,0));   // страховка от циклов
          return rows;
        },

      // «Не на схеме»: точки, которые не стоят ни на одной линии
      // (после П5 новые такие не заводятся — остаются старые/после сбоев).
      structPointsOffScheme(){
          return (this.structPoints||[]).filter(p=>p.measured_edge_id==null);
        },

      structSearchNodes(){
          const q=(this.structQuery||'').trim().toLowerCase();
          if(!q) return [];
          return (this.structNodes||[]).filter(n=>!n.archived_at &&
            (String(n.name||'').toLowerCase().includes(q)
             || String(n.code||'').toLowerCase().includes(q)));
        },

      async _loadStructureTreeEnergy(){
          const ids=[...new Set((this.structEdges||[])
            .filter(e=>e.primary_point_id!=null).map(e=>e.primary_point_id))];
          if(!ids.length){ this.structTreeEnergy={}; return; }
          if(!this.ovFrom || !this.ovTo){
            const d=this._ovDefaultDates(); this.ovFrom=this.ovFrom||d.from; this.ovTo=this.ovTo||d.to;
          }
          try{
            const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
            const r=await fetch('/api/v2/reports/query', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({dimension:'point', scope_ids:ids,
                                     from:this.ovFrom, to:this.ovTo, timezone:tz})});
            if(!r.ok) return;
            const energy={};
            ((await r.json()).rows||[]).forEach(row=>{ energy[row.id]=row.result; });
            this.structTreeEnergy=energy;
          }catch(e){ /* энергия в дереве необязательна */ }
        },

      structEnergyText(en){
          if(!en) return '';
          if(en.value!=null) return this.fmt(en.value,1)+' кВт·ч';
          if(en.known_value!=null) return '≥'+this.fmt(en.known_value,1)+' кВт·ч';
          return 'нет данных';
        },

      async toggleStructArchived(){
          this.structShowArchived=!this.structShowArchived;
          if(this.structShowArchived) await this._loadArchivedPoints();
        },

      async _loadArchivedPoints(){
          try{
            const r=await fetch('/api/v2/points?include_archived=1');
            const all=r.ok ? await r.json() : [];
            this.structArchivedPoints=(Array.isArray(all)?all:[]).filter(p=>p.archived_at);
          }catch(e){ this.structArchivedPoints=[]; }
        },

      // Открыть карточку архивной точки (read-only, без действий).
      async openArchivedPoint(pointId){
          await this.openInspectorInPlace('point', pointId);
        },

      // ================================================================
      // Новый элемент схемы из «Структуры» (тот же POST, что и в «Плане v3»)
      // ================================================================
      async structCreateNodeSubmit(){
          const f=this.structNewNode;
          const name=(f.name||'').trim();
          if(!name){ f.error='Назовите элемент схемы'; return; }
          f.saving=true; f.error=null;
          try{
            const rev=await this._structCurrentRev();
            const r=await fetch('/api/v2/topology/nodes', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({code:'pv3-'+f.kind+'-'+Date.now(), name, kind:f.kind,
                                     expected_revision:rev})});
            const d=await r.json();
            if(!r.ok){ f.error=this._apiErrorMessage(d, r.status); return; }
            this.showToast('«'+d.name+'» ('+this.nodeKindLabel(d.kind)+') добавлен в схему', true);
            this.structNewNode={kind:f.kind, name:'', saving:false, error:null};
            this.structNewNodeOpen=false;
            await this._loadStructureLists();
            await this.openInspectorInPlace('node', d.id);
          }catch(e){ f.error='Ошибка сети: '+e; }
          finally{ f.saving=false; }
        },

      // ================================================================
      // П5. «+ Счётчик» в карточке узла
      // ================================================================
      openNodeMeterForm(presetDeviceId){
          if(!this.structInspector || this.structInspector.kind!=='node') return;
          if(!this.structKnownDevicesLoaded) this._loadKnownDevices();
          const kind=this.structInspector.data.kind;
          const f={
            node_id:this.structInspector.id,
            device_id:presetDeviceId||'', name:'', nameTouched:false,
            measures: kind==='source' ? 'outgoing' : 'input',
            out_target:'', new_load_name:'', fed_from_node_id:'',
            saving:false, error:null,
          };
          this.nodeMeterForm=f;
          this.nodeMeterOpen=true;
          f.out_target=this._nodeMeterDefaultOutTarget();
          if(presetDeviceId) this.nodeMeterDeviceChanged();
        },

      closeNodeMeterForm(){ this.nodeMeterOpen=false; },

      nodeMeterDeviceChanged(){
          const f=this.nodeMeterForm; if(!f || !this.nodeMeterOpen) return;
          if(!f.nameTouched) f.name = f.device_id ? this.structDeviceName(f.device_id) : '';
        },

      nodeMeterChoices(){
          const i=this.structInspector;
          if(!i || i.kind!=='node') return [];
          const k=i.data.kind;
          const inLabel = k==='panel' ? 'Ввод щита — всё, что щит потребляет'
                        : (k==='load' ? 'Входящую линию нагрузки'
                                      : 'Вход ответвления — всё, что через него проходит');
          const inC={value:'input', label:inLabel};
          const outC={value:'outgoing', label:'Отходящую линию'};
          if(k==='source') return [outC];
          if(k==='load') return [inC];
          return [inC, outC];
        },

      nodeMeterInLine(){
          const i=this.structInspector;
          return ((i&&i.lines)||[]).find(l=>l.direction==='in')||null;
        },

      nodeMeterOutLines(){
          const i=this.structInspector;
          return ((i&&i.lines)||[]).filter(l=>l.direction==='out' && l.primary_point_id==null);
        },

      // Откуда питается элемент, у которого входящей линии ещё нет: любой
      // ввод/щит/ответвление, кроме самого элемента и его потомков (иначе
      // получился бы цикл — сервер такое не пропустит).
      nodeMeterFeedSources(){
          const i=this.structInspector;
          if(!i || i.kind!=='node') return [];
          const banned=this._structDescendants(i.id); banned.add(i.id);
          return (this.structNodes||[]).filter(n=>!n.archived_at && n.kind!=='load' && !banned.has(n.id));
        },

      // Существующие нагрузки без входящей линии — к ним можно провести
      // отходящую линию со счётчиком.
      nodeMeterLoadTargets(){
          const i=this.structInspector;
          if(!i || i.kind!=='node') return [];
          const withIncoming=new Set((this.structEdges||[]).map(e=>e.to_node_id));
          return (this.structNodes||[]).filter(n=>!n.archived_at && n.kind==='load'
            && n.id!==i.id && !withIncoming.has(n.id));
        },

      _structDescendants(nodeId){
          const seen=new Set();
          const stack=[nodeId];
          while(stack.length){
            const cur=stack.pop();
            (this.structEdges||[]).forEach(e=>{
              if(e.from_node_id===cur && !seen.has(e.to_node_id)){
                seen.add(e.to_node_id); stack.push(e.to_node_id);
              }
            });
          }
          return seen;
        },

      _nodeMeterDefaultOutTarget(){
          const lines=this.nodeMeterOutLines();
          return lines.length ? ('line:'+lines[0].edge_id) : 'new';
        },

      nodeMeterInLineBusy(){
          const l=this.nodeMeterInLine();
          return !!(l && l.primary_point_id!=null);
        },

      nodeMeterCanSubmit(){
          const f=this.nodeMeterForm; if(!this.nodeMeterOpen || f.saving || !f.device_id) return false;
          if(f.measures==='input'){
            if(this.nodeMeterInLine()) return !this.nodeMeterInLineBusy();
            return !!f.fed_from_node_id;
          }
          if(f.out_target==='new') return !!(f.new_load_name||'').trim();
          return !!f.out_target;
        },

      async submitNodeMeter(){
          const f=this.nodeMeterForm; if(!this.nodeMeterOpen) return;
          if(!f.device_id){ f.error='Выберите прибор из списка свободных'; return; }
          const body={device_id:f.device_id, measures:f.measures};
          const nm=(f.name||'').trim();
          if(nm) body.name=nm;
          if(f.measures==='input'){
            if(!this.nodeMeterInLine()){
              if(!f.fed_from_node_id){ f.error='Укажите, откуда питается этот элемент'; return; }
              body.fed_from_node_id=Number(f.fed_from_node_id);
            }
          }else{
            const t=f.out_target||'';
            if(t.indexOf('line:')===0) body.line_id=Number(t.slice(5));
            else if(t.indexOf('node:')===0) body.to_node_id=Number(t.slice(5));
            else if(t==='new'){
              const ln=(f.new_load_name||'').trim();
              if(!ln){ f.error='Назовите новую нагрузку'; return; }
              body.new_load_name=ln;
            }else{ f.error='Выберите, какую линию измеряет счётчик'; return; }
          }
          f.saving=true; f.error=null;
          try{
            body.expected_revision=await this._structCurrentRev();
            const r=await fetch('/api/v2/topology/nodes/'+f.node_id+'/meters', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify(body)});
            const d=await r.json();
            // Черновик формы НЕ сбрасывается при отказе — пользователь правит
            // выбор и повторяет (текст отказа — человеческий, без служебных слов).
            if(!r.ok){ f.error=this._apiErrorMessage(d, r.status); return; }
            const pointName=d.point ? (d.point.name||d.point.code) : '';
            if(d.attached==='already'){
              this.showToast('Этот прибор уже стоит на этой линии («'+pointName+'»)', true);
            }else{
              this.showToast('Счётчик «'+pointName+'» поставлен'+
                (d.node ? ('; нагрузка «'+d.node.name+'» добавлена в схему') : ''), true);
            }
            this.nodeMeterOpen=false;
            if(d.created_edge || d.node) await this._schemePlaceAfterMeter(d, f.node_id);
            await this._loadStructureLists();
            if(this.planV3Current) await this.reloadPlanV3Items();
            else await this.openInspectorInPlace('node', f.node_id);
          }catch(e){ f.error='Ошибка сети: '+e; }
          finally{ f.saving=false; }
        },

      // Новая нагрузка и новая линия появляются и на плане: значок в свободном
      // месте рядом со щитом (П8, _planV3SpotNear — то же, что у «отходящей
      // линии») и линия между значками.
      // Только если открыт план и значок самого элемента на нём есть; иначе
      // новое остаётся в схеме, а на карту его ставят из списка «Не размещены».
      async _schemePlaceAfterMeter(d, nodeId){
          const plan=this.planV3Current;
          if(!plan) return;
          try{
            const itemOf=(nid)=>(plan.items||[]).find(it=>it.kind==='node' && it.node_id===nid);
            const parent=itemOf(nodeId);
            let newItem=null;
            if(d.node && parent && parent.geometry){
              const {x,y}=this._planV3SpotNear(plan, parent.geometry, d.node.name);
              const ir=await fetch('/api/v2/plans/'+plan.id+'/items', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({kind:'node', node_id:d.node.id, geometry:{x,y},
                  coord_space:this._planV3CoordSpace(), label:d.node.name})});
              if(ir.ok) newItem=await ir.json();
            }
            if(d.created_edge && d.edge){
              const find=(nid)=>itemOf(nid)||(newItem && newItem.node_id===nid ? newItem : null);
              const fromItem=find(d.edge.from_node_id), toItem=find(d.edge.to_node_id);
              if(fromItem && toItem){
                await fetch('/api/v2/plans/'+plan.id+'/edges', {
                  method:'POST', headers:{'Content-Type':'application/json'},
                  body: JSON.stringify({edge_id:d.edge.id, from_item_id:fromItem.id,
                                         to_item_id:toItem.id, view_kind:'structural'})});
              }
            }
          }catch(e){ /* значок на плане — удобство; схема уже записана */ }
        },

      // «Приборы без точки учёта» → поставить на выбранный элемент схемы.
      async structDeviceToNode(deviceId){
          const nodeId=Number((this.structDeviceTarget||{})[deviceId]);
          if(!nodeId) return;
          await this.openInspectorInPlace('node', nodeId);
          this.openNodeMeterForm(deviceId);
          this.structDeviceTarget[deviceId]='';
        },

      // ================================================================
      // П1. Подтверждаемые действия: архивировать/убрать/вывести/удалить
      // ================================================================
      askConfirm(opts){
          this.uiConfirm=Object.assign({
            kind:'', payload:{}, title:'', text:'', lines:[], okLabel:'Подтвердить',
            extraLabel:null, extraVariant:null, defaultVariant:null,
            busy:false, error:null,
          }, opts);
          this.uiConfirmOpen=true;
        },

      cancelConfirm(){ this.uiConfirmOpen=false; },

      async runConfirm(variant){
          const c=this.uiConfirm;
          if(!this.uiConfirmOpen || c.busy) return;
          c.busy=true; c.error=null;
          try{
            const ok=await this._runConfirmAction(c.kind, c.payload||{}, variant||null);
            if(ok) this.uiConfirmOpen=false;
          }catch(e){
            c.error='Ошибка сети: '+e;
          }finally{
            c.busy=false;
          }
        },

      _confirmFail(msg){
          if(this.uiConfirmOpen) this.uiConfirm.error=msg;
          return false;
        },

      async _runConfirmAction(kind, p, variant){
          const J={'Content-Type':'application/json'};
          if(kind==='archive_point'){
            const rev=await this._structCurrentRev();
            const r=await fetch('/api/v2/points/'+p.point_id, {
              method:'PATCH', headers:J,
              body: JSON.stringify({archived:true, expected_revision:rev})});
            const d=await r.json();
            if(!r.ok) return this._confirmFail(this._apiErrorMessage(d, r.status));
            this.showToast(p.done||'Точка архивирована: история сохранена', true);
            await this._afterSchemeChange({closeInspector: this._inspectorIs('point', p.point_id)});
            return true;
          }
          if(kind==='remove_item'){
            const r=await fetch('/api/v2/plans/'+p.plan_id+'/items/'+p.item_id+'?edge_views=remove',
              {method:'DELETE'});
            if(!r.ok){
              let d={}; try{ d=await r.json(); }catch(e){}
              return this._confirmFail(this._apiErrorMessage(d, r.status));
            }
            this.showToast('Убрано с плана. В схеме остаётся — список «Не размещены на плане».', true);
            await this._afterSchemeChange({closeInspector:false});
            return true;
          }
          if(kind==='archive_node'){
            const rev=await this._structCurrentRev();
            const body={archived:true, expected_revision:rev};
            if(variant==='with_lines') body.retire_edges=true;
            const r=await fetch('/api/v2/topology/nodes/'+p.node_id, {
              method:'PATCH', headers:J, body: JSON.stringify(body)});
            const d=await r.json();
            if(!r.ok){
              // Отказ называет мешающие линии. Если они ещё есть — предлагаем
              // «вместе с линиями» (одной ревизией) и не закрываем окно.
              if(d && (d.fields||[]).indexOf('retire_edges')!==-1 && this.uiConfirmOpen){
                this.uiConfirm.extraLabel='Вывести линии из работы и архивировать';
                this.uiConfirm.extraVariant='with_lines';
              }
              return this._confirmFail(this._apiErrorMessage(d, r.status));
            }
            this.showToast('«'+p.name+'» архивирован: история сохранена', true);
            await this._afterSchemeChange({closeInspector:true});
            return true;
          }
          if(kind==='retire_edge'){
            const rev=await this._structCurrentRev();
            const r=await fetch('/api/v2/topology/edges/'+p.edge_id, {
              method:'PATCH', headers:J,
              body: JSON.stringify({retire:true, expected_revision:rev})});
            const d=await r.json();
            if(!r.ok) return this._confirmFail(this._apiErrorMessage(d, r.status));
            this.showToast('Линия выведена из работы: история сохранена', true);
            await this._afterSchemeChange({closeInspector:true});
            return true;
          }
          if(kind==='delete_plan'){
            const r=await fetch('/api/v2/plans/'+p.plan_id, {method:'DELETE'});
            if(!r.ok){
              let d={}; try{ d=await r.json(); }catch(e){}
              return this._confirmFail(this._apiErrorMessage(d, r.status));
            }
            this.showToast('План удалён. Схема, счётчики и история остаются.', true);
            this.closeInspector();
            this.planV3CurrentId=null; this.planV3Current=null;
            this._planV3ClearLayers();
            await this.loadPlanV3List();
            await this._loadStructureLists();
            return true;
          }
          return this._confirmFail('неизвестное действие: '+kind);
        },

      _inspectorIs(kind, id){
          const i=this.structInspector;
          return !!(i && i.kind===kind && i.id===id);
        },

      async _afterSchemeChange(opts){
          if(opts && opts.closeInspector) this.closeInspector();
          await this._loadStructureLists();
          if(this.planV3Current) await this.reloadPlanV3Items();
          else if(this.structInspector && !(opts && opts.closeInspector)){
            await this.openInspectorInPlace(this.structInspector.kind, this.structInspector.id);
          }
          if(this.structShowArchived) await this._loadArchivedPoints();
          if(this.tab==='overviewv2') this.loadOverviewV2Snapshot();
        },

      // ---- точка / счётчик
      askArchivePoint(point){
          this.askConfirm({
            kind:'archive_point', payload:{point_id:point.point_id, done:'Точка «'+point.name+'» архивирована'},
            title:'Архивировать точку «'+point.name+'»?',
            text:'История сохраняется; из текущего состава, итогов и балансов точка '+
                 'уходит с этого момента. Привязка прибора закроется — прибор освободится '+
                 'и его можно будет поставить заново.',
            okLabel:'Архивировать точку',
          });
        },

      askRemoveMeter(m){
          this.askConfirm({
            kind:'archive_point',
            payload:{point_id:m.point_id, done:'Счётчик «'+m.name+'» убран: история сохранена'},
            title:'Убрать счётчик «'+m.name+'»?',
            text:'Точка учёта архивируется: история сохраняется; из текущего состава, '+
                 'итогов и балансов она уходит с этого момента. Линия остаётся в схеме без '+
                 'счётчика, прибор освобождается.',
            okLabel:'Убрать счётчик',
          });
        },

      // ---- элемент схемы
      nodePlanItem(nodeId){
          return ((this.planV3Current&&this.planV3Current.items)||[])
            .find(it=>it.kind==='node' && it.node_id===nodeId)||null;
        },

      askRemoveNodeFromPlan(){
          const i=this.structInspector;
          if(!i || i.kind!=='node' || !this.planV3Current) return;
          const item=this.nodePlanItem(i.id);
          if(!item) return;
          this.askConfirm({
            kind:'remove_item', payload:{plan_id:this.planV3Current.id, item_id:item.id},
            title:'Убрать «'+i.data.name+'» с плана?',
            text:'Уберётся только значок на плане «'+this.planV3Current.name+'» и нарисованные '+
                 'к нему линии. Сам элемент, его линии и счётчики остаются в схеме; '+
                 'его можно снова разместить из списка «Не размещены на плане».',
            okLabel:'Убрать с плана',
          });
        },

      askArchiveNode(){
          const i=this.structInspector;
          if(!i || i.kind!=='node') return;
          const touching=(this.structEdges||[]).filter(
            e=>e.from_node_id===i.id || e.to_node_id===i.id);
          const lines=touching.map(e=>(e.name||(this.structNodeName(e.from_node_id)+' → '
            +this.structNodeName(e.to_node_id)))+
            (e.primary_point_id!=null ? (' (счётчик «'+this.structPointName(e.primary_point_id)+'»)') : ''));
          const c={
            kind:'archive_node', payload:{node_id:i.id, name:i.data.name},
            title:'Архивировать «'+i.data.name+'»?',
          };
          if(lines.length){
            c.text='У этого элемента есть действующие линии — они мешают архивации. '+
                   'Их можно вывести из работы вместе с ним одним действием: линии и их '+
                   'счётчики перестанут участвовать в итогах и балансах с этого момента, '+
                   'история сохранится.';
            c.lines=lines;
            c.okLabel='Вывести линии из работы и архивировать';
            c.payload.with_lines=true;
          }else{
            c.text='Элемент уйдёт из схемы и со всех планов. История сохраняется.';
            c.okLabel='Архивировать узел';
          }
          this.askConfirm(c);
          // С линиями единственная кнопка — «вместе с линиями» (вариант
          // выбираем сразу, а не после отказа сервера: отказ остаётся запасным).
          if(lines.length) this.uiConfirm.defaultVariant='with_lines';
        },

      askRetireEdge(edgeId){
          const e=(this.structEdges||[]).find(x=>x.id===edgeId);
          const label=e ? (e.name||(this.structNodeName(e.from_node_id)+' → '+this.structNodeName(e.to_node_id)))
                        : ('#'+edgeId);
          this.askConfirm({
            kind:'retire_edge', payload:{edge_id:edgeId},
            title:'Вывести линию «'+label+'» из работы?',
            text:'С этого момента линия не участвует в итогах и балансах; история сохраняется. '+
                 'Изображение линии на планах убирается.',
            okLabel:'Вывести из работы',
          });
        },

      askDeletePlan(){
          const p=this.planV3Current; if(!p) return;
          this.askConfirm({
            kind:'delete_plan', payload:{plan_id:p.id},
            title:'Удалить план «'+p.name+'»?',
            text:'Удаляется только представление: план и расстановка на нём. '+
                 'Схема, линии, счётчики, точки учёта и история остаются.',
            okLabel:'Удалить план',
          });
        },
    };
  });
})();


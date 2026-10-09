// plan-v3.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
//
// Модульные переменные карты Leaflet — ВНЕ IIFE, обычные глобальные `let`
// верхнего уровня скрипта, как и раньше в index.html (см.
// docs/agent-guides/frontend.md про Leaflet и Proxy-обёртку Alpine). Это отклонение от буквального примера
// в §3.2 ТЗ (там всё внутри одной IIFE): tests/browser/test_b02_planv3_clean.py
// обращается к _planV3Map напрямую через page.evaluate(), то есть ожидает
// его в глобальной области видимости страницы. Спрятать его в IIFE —
// значит сломать этот тест и потерять доступ, который был всегда (`let`
// верхнего уровня classic-скрипта не создаёт свойство window, но остаётся
// в общей глобальной лексической области — то же самое, что и сейчас).
let _planV3Map=null;
let _planV3ImageLayer=null;
const _planV3ItemLayers=new Map();
const _planV3EdgeLayers=new Map();
let _planV3NeedsFit=null;

(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function planV3Part() {
    return {
      planV3Inited:false,

      planV3Loading:false,

      planV3Saving:false,

      planV3Plans:[],

      planV3CurrentId:null,

      planV3Current:null,

      planV3NewPlanName:'',

      planV3Tool:null,

      planV3PendingNodeForm:{kind:'panel', name:''},

      planV3EdgeFirstItem:null,

      planV3ZoneGroupId:'',

      planV3DrawingZone:false,

      planV3AddConsumerName:'',

      planV3AddConsumerError:null,

      planV3NodeCardError:null,

      planV3EdgeCardError:null,

      planV3AttachEdgeId:null,

      planV3AttachDeviceId:'',

      planV3AttachError:null,

      planV3PanelUnplacedBusy:null,

      planV3PlacingExistingNodeId:null,

      planV3NewZoneGroupName:'',

      planV3NewZoneGroupOpen:false,

      planV3NewZoneGroupError:null,

      planV3NodePlaced(nodeId){
          return !!(this.planV3Current && this.planV3Current.items || [])
            .find(it=>it.kind==='node' && it.node_id===nodeId);
        },

      planV3PointOnLine(pointId){
          return (this.structEdges||[]).some(e=>e.primary_point_id===pointId);
        },

      planV3DevicesWithoutPoint(){
          const bound=new Set((this.structPoints||[])
            .filter(p=>p.bound && p.meter_device_id)
            .map(p=>p.meter_device_id));
          return (this.structKnownDevices||[]).filter(d=>!bound.has(d.device_id));
        },

      async planV3SaveEdgeCard(){
          if(!this.structInspector || this.structInspector.kind!=='edge') return;
          this.planV3EdgeCardError=null;
          const id=this.structInspector.id;
          const ratedRaw=(this.structInspector.ratedDraft||'').toString().trim();
          try{
            const rev=await this._structCurrentRev();
            const r=await fetch('/api/v2/topology/edges/'+id, {
              method:'PATCH', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({
                name: this.structInspector.nameDraft || null,
                rated_current_a: ratedRaw==='' ? null : Number(ratedRaw),
                cable_note: this.structInspector.cableDraft || null,
                expected_revision: rev,
              })});
            const d=await r.json();
            if(!r.ok){ this.planV3EdgeCardError=this._apiErrorMessage(d, r.status); return; }
            this.showToast('Линия обновлена', true);
            await this._loadStructureLists();
            if(this.planV3Current) await this.reloadPlanV3Items();
            else await this.openInspectorInPlace('edge', id);
          }catch(e){ this.planV3EdgeCardError='Ошибка сети: '+e; }
        },

      planV3ToggleAttachForm(edgeId){
          this.planV3AttachEdgeId = this.planV3AttachEdgeId===edgeId ? null : edgeId;
          this.planV3AttachDeviceId=''; this.planV3AttachError=null;
        },

      async planV3AttachNewPointSubmit(edgeId){
          const deviceId=(this.planV3AttachDeviceId||'').trim();
          if(!deviceId){ this.planV3AttachError='Укажите device_id прибора'; return; }
          this.planV3AttachError=null;
          try{
            const rev=await this._structCurrentRev();
            const r=await fetch('/api/v2/topology/edges/'+edgeId+'/attach-new-point', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({device_id: deviceId, expected_revision: rev})});
            const d=await r.json();
            if(!r.ok){ this.planV3AttachError=this._apiErrorMessage(d, r.status); return; }
            const pointName=d.point ? (d.point.name||d.point.code) : '';
            this.showToast(
              d.attached==='already' ? 'Уже подключена ('+pointName+')'
                                      : 'Точка «'+pointName+'» подключена и поставлена на линию',
              true);
            this.planV3AttachEdgeId=null; this.planV3AttachDeviceId='';
            await this._loadStructureLists();
            this._planV3RenderItems();
            if(this.structInspector){
              await this.openInspectorInPlace(this.structInspector.kind, this.structInspector.id);
            }
          }catch(e){ this.planV3AttachError='Ошибка сети: '+e; }
        },

      async _planV3RefreshNodeLiveNow(){
          if(!this.structInspector || this.structInspector.kind!=='node') return;
          const nodeId=this.structInspector.id;
          const pointIds=[...new Set((this.structInspector.lines||[])
            .filter(l=>l.primary_point_id!=null).map(l=>l.primary_point_id))];
          if(!pointIds.length) return;
          try{
            const r=await fetch('/api/v2/snapshot?point_ids='+pointIds.join(','));
            if(!r.ok) return;
            const pts=(await r.json()).points||[];
            if(!this.structInspector || this.structInspector.kind!=='node' || this.structInspector.id!==nodeId) return;
            const live={};
            pts.forEach(p=>{ live[p.point_id]={power_w:p.power_w, current_a:p.current_a}; });
            this.structInspector.lineLive=live;
          }catch(e){ /* необязательно — просто не покажем P-now */ }
        },

      async _planV3LoadNodeLineEnergy(){
          if(!this.structInspector || this.structInspector.kind!=='node') return;
          const nodeId=this.structInspector.id;
          const pointIds=[...new Set((this.structInspector.lines||[])
            .filter(l=>l.primary_point_id!=null).map(l=>l.primary_point_id))];
          if(!pointIds.length) return;
          if(!this.ovFrom || !this.ovTo){
            const d=this._ovDefaultDates(); this.ovFrom=this.ovFrom||d.from; this.ovTo=this.ovTo||d.to;
          }
          try{
            const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
            const r=await fetch('/api/v2/reports/query', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({dimension:'point', scope_ids:pointIds,
                                     from:this.ovFrom, to:this.ovTo, timezone:tz})});
            if(!r.ok) return;
            const rows=(await r.json()).rows||[];
            if(!this.structInspector || this.structInspector.kind!=='node' || this.structInspector.id!==nodeId) return;
            const energy={};
            rows.forEach(row=>{ energy[row.id]=row.result; });
            this.structInspector.lineEnergy=energy;
          }catch(e){ /* необязательно */ }
        },

      async _planV3LoadNodeBalance(nodeId){
          if(!this.ovFrom || !this.ovTo){
            const d=this._ovDefaultDates(); this.ovFrom=this.ovFrom||d.from; this.ovTo=this.ovTo||d.to;
          }
          if(this.structInspector && this.structInspector.id===nodeId) this.structInspector.balanceLoading=true;
          try{
            const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
            const r=await fetch('/api/v2/topology/nodes/'+nodeId+'/balance', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({from:this.ovFrom, to:this.ovTo, timezone:tz})});
            const d=await r.json();
            if(!this.structInspector || this.structInspector.kind!=='node' || this.structInspector.id!==nodeId) return;
            this.structInspector.balanceLoading=false;
            if(!r.ok){ this.structInspector.balanceError=this._apiErrorMessage(d, r.status); this.structInspector.balance=null; return; }
            this.structInspector.balance=d; this.structInspector.balanceError=null;
          }catch(e){
            if(this.structInspector && this.structInspector.kind==='node' && this.structInspector.id===nodeId){
              this.structInspector.balanceLoading=false;
              this.structInspector.balanceError='Ошибка сети: '+e;
            }
          }
        },

      async _planV3PollLiveCard(){
          if(!this.structInspector) return;
          if(this.structInspector.kind==='edge'){
            const edgeId=this.structInspector.id;
            const {currentA, loadPct, phases, power_w}=await this._planEdgeLiveMetrics(this.structInspector.data);
            if(this.structInspector && this.structInspector.kind==='edge' && this.structInspector.id===edgeId){
              this.structInspector.currentA=currentA; this.structInspector.loadPct=loadPct;
              this.structInspector.phases=phases; this.structInspector.power_w=power_w;
            }
          } else if(this.structInspector.kind==='node'){
            await this._planV3RefreshNodeLiveNow();
          }
        },

      async openPlanV3Tab(){
          if(!_planV3Map) this.initPlanV3Map();
          if(!this.planV3Inited){
            this.planV3Inited=true;
            const loads=[this.loadPlanV3List()];
            if(!this.structInited){ this.structInited=true; loads.push(this._loadStructureLists()); }
            // Э3/B4: «Приборы без точки учёта» в правой панели, когда ничего
            // не выбрано — тот же список, что структура грузит по требованию
            // (см. openStructureTab), здесь нужен сразу.
            if(!this.structKnownDevicesLoaded) loads.push(this._loadKnownDevices());
            await Promise.all(loads);
          }
          // Партия 7, Этап 3, B1: контейнер карты мог быть невидим (display:none
          // за x-show), когда _planV3RenderBase() уже пытался вписать план —
          // Leaflet вписывает в нулевой размер и остаётся на zoom=-4 насовсем.
          // Здесь, после $nextTick (контейнер уже получил реальный размер),
          // довписываем то, что не удалось раньше (_planV3NeedsFit).
          this.$nextTick(()=>{ if(_planV3Map){ _planV3Map.invalidateSize(); this._planV3TryFit(); } });
        },

      initPlanV3Map(){
          if(_planV3Map) return;
          const el=this.$refs.planV3Map;
          // Э3/B1, доп. правка: zoomSnap:0 — без этого Leaflet округляет
          // fitBounds() до ЦЕЛОГО уровня зума (zoomSnap=1 по умолчанию) вниз
          // до ближайшего, который ТОЧНО помещается — план у которого
          // соотношение сторон не попадает ровно на степень двойки от
          // контейнера, вписывается с запасом ДО ПОЛОВИНЫ контейнера пустым
          // полем (один шаг зума — это 2×), а не "впритык". Итог на браузерном
          // прогоне test_b02: клик в верхнюю левую четверть ХОЛСТА (контейнера
          // карты) попадал за пределы реально нарисованного плана ("Точка вне
          // границ плана"), хотя пользователь целился именно в план — то же
          // самое произошло бы и у живого пользователя на объекте с планом
          // нестандартных пропорций. С zoomSnap:0 fitBounds() вписывает план
          // впритык (дробный зум), без резкого запаса вдвое.
          _planV3Map=L.map(el, {crs:L.CRS.Simple, minZoom:-4, zoomSnap:0, zoomControl:true,
                                attributionControl:false});
          _planV3Map.setView([0,0],0);
          _planV3Map.on('click', (e)=>this._planV3OnMapClick(e));
          _planV3Map.on('pm:create', (e)=>this._planV3OnPmCreate(e));
          window.addEventListener('resize', ()=>{ if(_planV3Map) _planV3Map.invalidateSize(); });
          // Э3/B1, доп. правка (найдено браузерным прогоном test_b02 ПОСЛЕ
          // первой версии фикса выше): $nextTick после смены вкладки/выбора
          // плана НЕ гарантирует, что x-show родительского контейнера
          // (planV3Plans.length>0) уже применился к DOM в тот же тик — на
          // практике Alpine иногда применяет его позже. Тогда _planV3TryFit(),
          // вызванный из $nextTick в openPlanV3Tab()/selectPlanV3(), видит
          // контейнер ещё нулевого размера и оставляет _planV3NeedsFit висеть
          // НАВСЕГДА — больше никто не пробует ещё раз: план визуально
          // виден, но карта осталась на zoom/центре из setView([0,0],0) выше,
          // а не вписанной по границам — из-за этого клик по видимой части
          // холста давал координату вне логических границ плана ("Точка вне
          // границ плана"), хотя пользователь кликал ровно там, где нарисован
          // план. ResizeObserver — сигнал, не зависящий от того, чем именно
          // вызвано появление контейнера (Alpine, смена вкладки, ресайз окна,
          // сворачивание/разворачивание DevTools): на каждое изменение размера
          // пробуем довписать отложенные границы; когда вписывать нечего
          // (_planV3NeedsFit уже null) — _planV3TryFit() дешёвый no-op.
          if(window.ResizeObserver){
            new ResizeObserver(()=>{
              if(_planV3Map){ _planV3Map.invalidateSize(); this._planV3TryFit(); }
            }).observe(el);
          }
        },

      async loadPlanV3List(){
          this.planV3Loading=true;
          try{
            const r=await fetch('/api/v2/plans');
            const d=await r.json();
            this.planV3Plans=Array.isArray(d)?d:[];
            if(this.planV3Plans.length>0 && !this.planV3CurrentId){
              await this.selectPlanV3(this.planV3Plans[0].id);
            }
          }catch(e){ console.error(e); }
          this.planV3Loading=false;
        },

      async planV3CreateEmptyPlan(){
          const name=(this.planV3NewPlanName||'').trim() || 'План v3';
          this.planV3Saving=true;
          try{
            const fd=new FormData();
            fd.append('name', name); fd.append('plan_kind', 'single_line');
            fd.append('canvas_width', '2000'); fd.append('canvas_height', '1200');
            const r=await fetch('/api/v2/plans', {method:'POST', body:fd});
            const d=await r.json();
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            this.planV3NewPlanName='';
            await this.loadPlanV3List();
            await this.selectPlanV3(d.id);
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
          finally{ this.planV3Saving=false; }
        },

      async selectPlanV3(id){
          if(!id) return;
          this.planV3CancelTool();
          this.planV3CurrentId=id;
          this.closeInspector();
          try{
            const r=await fetch('/api/v2/plans/'+id);
            if(!r.ok){ this.showToast('Не удалось загрузить план', false); return; }
            this.planV3Current=await r.json();
          }catch(e){ console.error(e); return; }
          this._planV3RenderBase();
          // Б1: контейнер мог стать видимым ТОЛЬКО что (например, только что
          // созданный первый план — x-show="planV3Plans.length>0" переключился
          // синхронно с этим же тиком) — $nextTick ждёт, пока Alpine применит
          // DOM-патч, и тогда пробуем вписать ещё раз.
          this.$nextTick(()=>this._planV3TryFit());
        },

      async reloadPlanV3Items(){
          if(!this.planV3Current) return;
          try{
            const r=await fetch('/api/v2/plans/'+this.planV3Current.id);
            if(!r.ok) return;
            this.planV3Current=await r.json();
          }catch(e){ console.error(e); return; }
          this._planV3RenderItems();
          if(this.structInspector && (this.structInspector.kind==='node' || this.structInspector.kind==='edge')){
            await this.openInspectorInPlace(this.structInspector.kind, this.structInspector.id);
          }
        },

      _planV3EffectiveDims(plan){
          if(plan.image_width && plan.image_height) return [plan.image_width, plan.image_height];
          return [plan.canvas_width||2000, plan.canvas_height||1200];
        },

      _planV3XyToLatLng(x, y, height){ return [height-y, x]; },

      _planV3LatLngToXy(latlng, height){ return [latlng.lng, height-latlng.lat]; },

      _planV3CoordSpace(){
          return this.planV3Current && this.planV3Current.plan_kind==='floor'
            ? 'image_px_xy_v2' : 'canvas_xy_v2';
        },

      planV3SetTool(tool){
          this.planV3CancelTool();
          this.planV3Tool=tool;
          if(tool==='node'){ this.planV3PendingNodeForm={kind:'panel', name:''}; }
        },

      planV3CancelTool(){
          this.planV3Tool=null;
          this.planV3EdgeFirstItem=null;
          this.planV3ZoneGroupId='';
          this.planV3PlacingExistingNodeId=null;
          this._planV3CancelDrawZone();
        },

      _planV3OnMapClick(e){
          if(!this.planV3Current) return;
          if(this.planV3Tool==='node'){
            const name=(this.planV3PendingNodeForm.name||'').trim();
            if(!name){ this.showToast('Укажите название узла', false); return; }
            const [W,H]=this._planV3EffectiveDims(this.planV3Current);
            const [x,y]=this._planV3LatLngToXy(e.latlng, H);
            if(x<0||x>W||y<0||y>H){ this.showToast('Точка вне границ плана', false); return; }
            this.planV3PlaceNode(x,y);
            return;
          }
          // Э3/B4: «Разместить на карте» у уже заведённого, но ещё не
          // нанесённого узла — тот же клик по карте, что и «+ Узел», только
          // без создания нового узла (id уже есть, см. planV3StartPlaceExistingNode).
          if(this.planV3Tool==='node-existing' && this.planV3PlacingExistingNodeId!=null){
            const [W,H]=this._planV3EffectiveDims(this.planV3Current);
            const [x,y]=this._planV3LatLngToXy(e.latlng, H);
            if(x<0||x>W||y<0||y>H){ this.showToast('Точка вне границ плана', false); return; }
            this.planV3PlaceExistingNode(x,y);
          }
        },

      planV3StartPlaceExistingNode(nodeId){
          this.planV3CancelTool();
          this.planV3Tool='node-existing';
          this.planV3PlacingExistingNodeId=nodeId;
          this.showToast('Кликните на карте, чтобы разместить узел', true);
        },

      async planV3PlaceExistingNode(x,y){
          const nodeId=this.planV3PlacingExistingNodeId;
          if(nodeId==null) return;
          const node=(this.structNodes||[]).find(n=>n.id===nodeId);
          this.planV3PanelUnplacedBusy=nodeId;
          try{
            const ir=await fetch('/api/v2/plans/'+this.planV3Current.id+'/items', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({kind:'node', node_id:nodeId, geometry:{x,y},
                coord_space:this._planV3CoordSpace(), label:node?node.name:''})});
            const id_=await ir.json();
            if(!ir.ok){ this.showToast(this._apiErrorMessage(id_, ir.status), false); return; }
            this.showToast('Узел «'+(node?node.name:nodeId)+'» размещён на плане', true);
            this.planV3PlacingExistingNodeId=null; this.planV3Tool=null;
            await this._loadStructureLists();
            await this.reloadPlanV3Items();
            await this.openInspectorInPlace('node', nodeId);
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
          finally{ this.planV3PanelUnplacedBusy=null; }
        },

      async planV3PlaceNode(x,y){
          const form=this.planV3PendingNodeForm;
          const name=(form.name||'').trim();
          try{
            const nr=await fetch('/api/v2/topology/nodes', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({code:'pv3-'+form.kind+'-'+Date.now(), name, kind:form.kind})});
            const nd=await nr.json();
            if(!nr.ok){ this.showToast(this._apiErrorMessage(nd, nr.status), false); return; }
            const ir=await fetch('/api/v2/plans/'+this.planV3Current.id+'/items', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({kind:'node', node_id:nd.id, geometry:{x,y},
                coord_space:this._planV3CoordSpace(), label:name})});
            const id_=await ir.json();
            if(!ir.ok){ this.showToast(this._apiErrorMessage(id_, ir.status), false); return; }
            this.showToast('Узел «'+name+'» размещён', true);
            this.planV3PendingNodeForm={kind:form.kind, name:''};
            await this._loadStructureLists();
            await this.reloadPlanV3Items();
            await this.openInspectorInPlace('node', nd.id);
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
        },

      async _planV3OnItemDragEnd(item, latlng, height){
          const [x,y]=this._planV3LatLngToXy(latlng, height);
          try{
            const r=await fetch('/api/v2/plans/'+this.planV3Current.id+'/items/'+item.id, {
              method:'PATCH', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({geometry:{x,y}, coord_space:item.coord_space})});
            if(!r.ok){ const d=await r.json(); this.showToast(this._apiErrorMessage(d, r.status), false); }
            await this.reloadPlanV3Items();
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
        },

      _planV3OnNodeMarkerClick(item){
          if(this.planV3Tool==='edge'){
            if(!this.planV3EdgeFirstItem){
              this.planV3EdgeFirstItem=item;
            }else{
              const fromItem=this.planV3EdgeFirstItem;
              this.planV3EdgeFirstItem=null;
              if(fromItem.node_id===item.node_id){
                this.showToast('Нельзя соединить узел с самим собой', false); return;
              }
              this.planV3ConnectNodes(fromItem, item);
            }
            return;
          }
          this.openInspectorInPlace('node', item.node_id);
        },

      async planV3ConnectNodes(fromItem, toItem){
          try{
            const rev=await this._structCurrentRev();
            const r=await fetch('/api/v2/topology/edges/connect', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({from_node_id: fromItem.node_id, to_node_id: toItem.node_id,
                                     expected_revision: rev})});
            const d=await r.json();
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            await fetch('/api/v2/plans/'+this.planV3Current.id+'/edges', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({edge_id:d.id, from_item_id:fromItem.id, to_item_id:toItem.id,
                                     view_kind:'structural'})});
            this.showToast('Линия проведена', true);
            await this._loadStructureLists();
            await this.reloadPlanV3Items();
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
          finally{ this.planV3Tool=null; }
        },

      async planV3CreateGroupInline(){
          const name=(this.planV3NewZoneGroupName||'').trim();
          if(!name) return;
          this.planV3NewZoneGroupError=null;
          try{
            const r=await fetch('/api/v2/groups', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({name})});
            const d=await r.json();
            if(!r.ok){ this.planV3NewZoneGroupError=this._apiErrorMessage(d, r.status); return; }
            this.structGroups.push(d);
            this.planV3ZoneGroupId=d.id;
            this.planV3NewZoneGroupOpen=false; this.planV3NewZoneGroupName='';
            this.showToast('Группа «'+d.name+'» создана', true);
          }catch(e){ this.planV3NewZoneGroupError='Ошибка сети: '+e; }
        },

      planV3StartDrawZone(){
          if(!this.planV3ZoneGroupId || !_planV3Map) return;
          this.planV3DrawingZone=true;
          if(_planV3Map.doubleClickZoom) _planV3Map.doubleClickZoom.disable();
          _planV3Map.pm.enableDraw('Polygon', {snappable:true, finishOn:'dblclick'});
        },

      _planV3CancelDrawZone(){
          if(_planV3Map && _planV3Map.pm) _planV3Map.pm.disableDraw('Polygon');
          if(_planV3Map && _planV3Map.doubleClickZoom) _planV3Map.doubleClickZoom.enable();
          this.planV3DrawingZone=false;
        },

      async _planV3OnPmCreate(e){
          if(e.shape!=='Polygon' || !this.planV3DrawingZone || !this.planV3Current) return;
          const rings=e.layer.getLatLngs();
          const ring=Array.isArray(rings[0]) ? rings[0] : rings;
          try{_planV3Map.removeLayer(e.layer);}catch(err){}
          const groupId=this.planV3ZoneGroupId;
          this._planV3CancelDrawZone();
          if(ring.length<3){ this.showToast('Контур должен содержать минимум 3 точки', false); return; }
          const [,H]=this._planV3EffectiveDims(this.planV3Current);
          const geometry=ring.map(ll=>this._planV3LatLngToXy(ll, H));
          try{
            const r=await fetch('/api/v2/plans/'+this.planV3Current.id+'/items', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({kind:'group', group_id:groupId, geometry,
                                     coord_space:this._planV3CoordSpace()})});
            const d=await r.json();
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            this.showToast('Зона нанесена', true);
            this.planV3Tool=null; this.planV3ZoneGroupId='';
            await this.reloadPlanV3Items();
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
        },

      async planV3SaveNodeCard(){
          if(!this.structInspector || this.structInspector.kind!=='node') return;
          this.planV3NodeCardError=null;
          try{
            const rev=await this._structCurrentRev();
            const r=await fetch('/api/v2/topology/nodes/'+this.structInspector.id, {
              method:'PATCH', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({
                name: this.structInspector.nameDraft,
                location_text: this.structInspector.locationTextDraft,
                kind: this.structInspector.kindDraft,
                expected_revision: rev,
              })});
            const d=await r.json();
            if(!r.ok){ this.planV3NodeCardError=this._apiErrorMessage(d, r.status); return; }
            this.showToast('Узел обновлён', true);
            const id=this.structInspector.id;
            await this._loadStructureLists();
            // reloadPlanV3Items() сама перечитывает открытую карточку того же
            // узла (structInspector.id уже равен id здесь) — отдельный
            // openInspectorInPlace не нужен, не дублируем запрос (Э3/B2).
            if(this.planV3Current) await this.reloadPlanV3Items();
            else await this.openInspectorInPlace('node', id);
          }catch(e){ this.planV3NodeCardError='Ошибка сети: '+e; }
        },

      async planV3AssignMeter(edgeId, pointId){
          try{
            const rev=await this._structCurrentRev();
            const r=await fetch('/api/v2/topology/edges/'+edgeId, {
              method:'PATCH', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({primary_point_id: pointId, expected_revision: rev})});
            const d=await r.json();
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            this.showToast(pointId?'Счётчик назначен':'Счётчик снят', true);
            await this._loadStructureLists();
            // Линия на карте должна сразу перекраситься/перестать быть
            // пунктирной (Э3/B3) — без этого цвет обновлялся только при
            // следующей полной перезагрузке плана.
            this._planV3RenderItems();
            if(this.structInspector && (this.structInspector.kind==='node' || this.structInspector.kind==='edge')){
              await this.openInspectorInPlace(this.structInspector.kind, this.structInspector.id);
            }
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
        },

      async planV3AddConsumerSubmit(){
          if(!this.structInspector || this.structInspector.kind!=='node') return;
          const name=(this.planV3AddConsumerName||'').trim();
          if(!name){ this.planV3AddConsumerError='Укажите название'; return; }
          this.planV3AddConsumerError=null;
          try{
            const rev=await this._structCurrentRev();
            const r=await fetch('/api/v2/topology/nodes/'+this.structInspector.id+'/add-consumer', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({name, expected_revision: rev})});
            const d=await r.json();
            if(!r.ok){ this.planV3AddConsumerError=this._apiErrorMessage(d, r.status); return; }
            if(this.planV3Current){
              const parentItem=(this.planV3Current.items||[]).find(
                it=>it.kind==='node' && it.node_id===this.structInspector.id);
              const [W,H]=this._planV3EffectiveDims(this.planV3Current);
              let x=W/2, y=H/2;
              if(parentItem && parentItem.geometry){
                x=Math.min(W, parentItem.geometry.x+60); y=Math.min(H, parentItem.geometry.y+60);
              }
              const ir=await fetch('/api/v2/plans/'+this.planV3Current.id+'/items', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({kind:'node', node_id:d.node.id, geometry:{x,y},
                  coord_space:this._planV3CoordSpace(), label:d.node.name})});
              const itemD=await ir.json();
              if(ir.ok && parentItem){
                await fetch('/api/v2/plans/'+this.planV3Current.id+'/edges', {
                  method:'POST', headers:{'Content-Type':'application/json'},
                  body: JSON.stringify({edge_id:d.edge.id, from_item_id:parentItem.id,
                                         to_item_id:itemD.id, view_kind:'structural'})});
              }
            }
            this.showToast('Отходящая линия добавлена', true);
            this.planV3AddConsumerName='';
            const id=this.structInspector.id;
            await this._loadStructureLists();
            // reloadPlanV3Items() перечитывает уже открытую карточку родителя
            // (structInspector.id всё ещё id родителя здесь) сама — Э3/B2.
            if(this.planV3Current) await this.reloadPlanV3Items();
            else await this.openInspectorInPlace('node', id);
          }catch(e){ this.planV3AddConsumerError='Ошибка сети: '+e; }
        },

      _planV3ItemLayerFor(item, height){
          // Зона (учётная группа) — контур-полигон, кликом только подсказка,
          // без своей карточки (сама группа управляется на «Структуре»).
          if(item.kind==='group' && Array.isArray(item.geometry)){
            if(item.geometry.length<3) return null;
            const latlngs=item.geometry.map(([x,y])=>this._planV3XyToLatLng(x,y,height));
            const color=_planCssVar('--accent2');
            const layer=L.polygon(latlngs, {color, fillColor:color, fillOpacity:.18, weight:2});
            const group=(this.structGroups||[]).find(g=>g.id===item.group_id);
            layer.on('click', (e)=>{
              if(e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
              this.showToast(group?('Учётная группа: '+group.name):'Учётная группа', true);
            });
            return layer;
          }
          if(item.kind!=='node' || !item.geometry || typeof item.geometry.x!=='number') return null;
          const node=(this.structNodes||[]).find(n=>n.id===item.node_id);
          const colors={source:'#4fa8f5', panel:'#3dd6c0', junction:'#9aa0a8', load:'#f0a83c'};
          const color=colors[node?node.kind:'']||'#3dd6c0';
          const latlng=this._planV3XyToLatLng(item.geometry.x, item.geometry.y, height);
          const layer=L.circleMarker(latlng, {radius:9, color, fillColor:color, fillOpacity:.8, weight:2});
          // Э3/B3: постоянная подпись узла (не только по наведению) — иначе
          // на плане с десятком узлов не разобрать, где что, не кликая по
          // каждому. Leaflet вставляет текст как HTML — _planEscapeHtml (A45).
          if(node){
            layer.bindTooltip(_planEscapeHtml(node.name||node.code||''),
              {permanent:true, direction:'right', className:'planv3-node-tip'});
          }
          layer.on('click', (e)=>{
            if(e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
            this._planV3OnNodeMarkerClick(item);
          });
          // Перетаскивание — как у Плана v2, всегда включено (нет отдельного
          // режима редактирования — все действия здесь прямые).
          if(layer.pm){
            layer.pm.enableLayerDrag();
            layer.on('pm:dragend', ()=>{ this._planV3OnItemDragEnd(item, layer.getLatLng(), height); });
          }
          return layer;
        },

      _planV3RenderItems(){
          if(!_planV3Map || !this.planV3Current) return;
          _planV3ItemLayers.forEach(l=>{ try{_planV3Map.removeLayer(l);}catch(e){} });
          _planV3ItemLayers.clear();
          _planV3EdgeLayers.forEach(l=>{ try{_planV3Map.removeLayer(l);}catch(e){} });
          _planV3EdgeLayers.clear();
      
          const [,H]=this._planV3EffectiveDims(this.planV3Current);
          const items=this.planV3Current.items||[];
          items.forEach(it=>{
            const layer=this._planV3ItemLayerFor(it, H);
            if(layer){ layer.addTo(_planV3Map); _planV3ItemLayers.set(String(it.id), layer); }
          });
      
          (this.planV3Current.edges||[]).forEach(v=>{
            const fromIt=v.from_item_id!=null ? items.find(i=>i.id===v.from_item_id) : null;
            const toIt=v.to_item_id!=null ? items.find(i=>i.id===v.to_item_id) : null;
            if(!fromIt || !toIt || !fromIt.geometry || !toIt.geometry) return;
            const latlngs=[this._planV3XyToLatLng(fromIt.geometry.x, fromIt.geometry.y, H),
                           this._planV3XyToLatLng(toIt.geometry.x, toIt.geometry.y, H)];
            const edge=(this.structEdges||[]).find(e2=>e2.id===v.edge_id);
            const metered=!!(edge && edge.primary_point_id);
            const color=metered ? _planCssVar('--ok') : '#e0995f';
            // Э3/B3: линии без счётчика — пунктиром, не только другим цветом
            // (различимо и без цвета, например при дальтонизме/ч-б печати).
            const line=L.polyline(latlngs, {color, weight:3, dashArray: metered?null:'6,6'});
            // Э3/B3: подпись по наведению — имя связи или «A → B» по концам.
            if(edge){
              const label=edge.name || edge.code ||
                (this.structNodeName(edge.from_node_id)+' → '+this.structNodeName(edge.to_node_id));
              line.bindTooltip(_planEscapeHtml(label), {sticky:true});
            }
            line.on('click', (e)=>{
              if(e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
              // Э3/B5: у линии теперь своя карточка (имя/номинал/кабель,
              // счётчик, живой ток/загрузка) — до этой партии клика было
              // некуда деть, счётчик назначался только из карточки узла.
              if(edge) this.openInspectorInPlace('edge', edge.id);
            });
            line.addTo(_planV3Map);
            _planV3EdgeLayers.set(String(v.id), line);
          });
        },

      _planV3RenderBase(){
          if(!_planV3Map || !this.planV3Current) return;
          if(_planV3ImageLayer){ try{_planV3Map.removeLayer(_planV3ImageLayer);}catch(e){} _planV3ImageLayer=null; }
          const pc=this.planV3Current;
          const [W,H]=this._planV3EffectiveDims(pc);
          const bounds=[[0,0],[H,W]];
          if(pc.plan_kind==='floor'){
            _planV3ImageLayer=L.imageOverlay('/api/v2/plans/'+pc.id+'/image', bounds).addTo(_planV3Map);
          }else{
            _planV3ImageLayer=L.rectangle(bounds, {color:_planCssVar('--line'), weight:1, fill:false, dashArray:'4,4'}).addTo(_planV3Map);
          }
          // Э3/B1: fitBounds НЕ вызывается напрямую здесь — если контейнер в
          // этот момент невидим/нулевого размера (x-show ещё не применился),
          // Leaflet вписывает в нулевой прямоугольник и дальше держит
          // zoom=-4 насовсем, даже когда контейнер потом появляется. Вместо
          // этого запоминаем границы и пробуем вписать сразу; если не вышло —
          // openPlanV3Tab()/selectPlanV3() довписывают через $nextTick, когда
          // контейнер уже получил реальный размер.
          _planV3NeedsFit=bounds;
          this._planV3RenderItems();
          this._planV3TryFit();
        },

      _planV3TryFit(){
          if(!_planV3Map || !_planV3NeedsFit) return false;
          const el=this.$refs.planV3Map;
          if(!el || el.offsetWidth===0 || el.offsetHeight===0) return false;
          const bounds=_planV3NeedsFit;
          _planV3NeedsFit=null;
          _planV3Map.invalidateSize();
          _planV3Map.fitBounds(bounds);
          return true;
        }
    };
  });
})();

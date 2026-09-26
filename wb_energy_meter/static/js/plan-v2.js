// plan-v2.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
// ---- модульные переменные/хелперы только этого экрана ----
let _planV2Map=null;
let _planV2ImageLayer=null;
const _planV2ItemLayers=new Map();
const _planV2EdgeLayers=new Map();
let _planV2DrawLayer=null;
function _planV2KindColor(kind){
  const colors={point:'#3dd6c0', location:'#8a7cff', group:'#f0a83c',
                node:'#4fa8f5', port:'#e05fae', annotation:'#9aa0a8'};
  return colors[kind] || '#3dd6c0';
}

(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function planV2Part() {
    return {
      planV2Plans:[],

      planV2CurrentId:null,

      planV2Current:null,

      planV2Loading:false,

      planV2Inited:false,

      planV2SelectedItem:null,

      planV2SelectedEdgeView:null,

      planV2EdgeInfo:null,

      planV2EditMode:false,

      planV2Draft:{items:{}, edges:{}},

      planV2History:[],

      planV2Future:[],

      planV2Saving:false,

      planV2ConfirmDiscard:false,

      planV2RevisionConflict:false,

      planV2Placing:false,

      planV2AddKind:'',

      planV2AddRefId:'',

      planV2AddTargetPlanId:'',

      planV2AddLabel:'',

      planV2NewItemSeq:0,

      planV2PlacingPolygon:false,

      planV2LocationAssignProposal:null,

      planV2AddingEdgeView:false,

      planV2NewEdgeSeq:0,

      planV2EdgeForm:{edgeId:'', fromItemKey:'', toItemKey:''},

      planV2DrawingEdgeKey:null,

      planV2HasPendingEdgeDraw:false,

      planV2PickersLoaded:false,

      planV2Points:[],

      planV2Locations:[],

      planV2Nodes:[],

      planV2Edges:[],

      initPlanV2Map(){
          if(_planV2Map) return;
          const el=this.$refs.planV2Map;
          _planV2Map=L.map(el, {crs:L.CRS.Simple, minZoom:-4, zoomControl:true,
                                attributionControl:false});
          _planV2Map.setView([0,0],0);
          _planV2Map.on('click', (e)=>this._planV2OnMapClick(e));
          _planV2Map.on('pm:create', (e)=>this._planV2OnPmCreate(e));
          window.addEventListener('resize', ()=>{ if(_planV2Map) _planV2Map.invalidateSize(); });
        },

      planV2EdgeViewLabel(v){
          const e=(this.structEdges||[]).find(x=>x.id===v.edge_id);
          if(!e) return 'Связь #'+v.edge_id;
          if(e.name) return e.name;
          if(e.code) return e.code;
          return this.structNodeName(e.from_node_id)+' → '+this.structNodeName(e.to_node_id);
        },

      async _planV2SelectEdgeView(v){
          this.planV2SelectedItem=null;
          this.planV2SelectedEdgeView=v;
          this._planV2LoadEdgeInfo(v.edge_id);
        },

      async _planV2LoadEdgeInfo(edgeId){
          this.planV2EdgeInfo={loading:true, edgeId};
          try{
            if(!this.structInited){ this.structInited=true; await this._loadStructureLists(); }
            const r=await fetch('/api/v2/topology/edges/'+edgeId);
            if(!r.ok) throw new Error('связь не найдена (HTTP '+r.status+')');
            const edge=await r.json();
            const {currentA, loadPct}=await this._planEdgeLiveMetrics(edge);
            if(this.planV2EdgeInfo && this.planV2EdgeInfo.edgeId===edgeId){
              this.planV2EdgeInfo={loading:false, edgeId, edge, currentA, loadPct};
            }
          }catch(e){
            this.planV2EdgeInfo={loading:false, edgeId, error:'Не удалось загрузить связь: '+e.message};
          }
        },

      async _planV2GoToItem(planId, itemId){
          this.tab = 'planv2';
          await this.openPlanV2Tab();
          await this.selectPlanV2(planId);
          setTimeout(()=>{
            try{
              const layer = _planV2ItemLayers.get(String(itemId));
              if(layer && _planV2Map){
                if(layer.getLatLng){ _planV2Map.panTo(layer.getLatLng()); if(layer.openPopup) layer.openPopup(); }
                else if(layer.getBounds){ _planV2Map.fitBounds(layer.getBounds()); }
              }
            }catch(e){ /* переход по плану best-effort — карточка уже открыта */ }
          }, 350);
        },

      async openPlanV2Tab(){
          if(!_planV2Map) this.initPlanV2Map();
          if(!this.planV2Inited){
            this.planV2Inited=true;
            // Партия 6, задача 6: узлы/точки нужны, чтобы подписи связей на
            // плане (planV2EdgeViewLabel) и карточка выбранной связи
            // (_planV2LoadEdgeInfo) сразу показывали имена, а не "#N", даже
            // если пользователь открыл План, минуя вкладку «Структура».
            const loads=[this.loadPlanV2List()];
            if(!this.structInited){ this.structInited=true; loads.push(this._loadStructureLists()); }
            await Promise.all(loads);
          }
          this.$nextTick(()=>{ if(_planV2Map) _planV2Map.invalidateSize(); });
        },

      async loadPlanV2List(){
          this.planV2Loading=true;
          try{
            const r=await fetch('/api/v2/plans');
            const d=await r.json();
            this.planV2Plans=Array.isArray(d)?d:[];
            if(this.planV2Plans.length>0 && !this.planV2CurrentId){
              await this.selectPlanV2(this.planV2Plans[0].id);
            }
          }catch(e){ console.error(e); }
          this.planV2Loading=false;
        },

      async selectPlanV2(id){
          if(!id) return;
          // Переключение плана с несохранённым черновиком отменяет черновик —
          // черновик привязан к конкретному плану и его canvas_revision, нести
          // его на другой план бессмысленно и опасно (см. §7.3).
          if(this.planV2PendingCount()>0){
            this.showToast('Черновик изменений отменён при смене плана', false);
          }
          this._planV2ResetDraft();
          this.planV2CurrentId=id;
          this.planV2SelectedItem=null;
          this.planV2SelectedEdgeView=null;
          this.planV2Placing=false;
          try{
            const r=await fetch('/api/v2/plans/'+id);
            if(!r.ok){ this.showToast('Не удалось загрузить план v2', false); return; }
            this.planV2Current=await r.json();
          }catch(e){ console.error(e); return; }
          this._planV2RenderBase();
        },

      _planV2ResetDraft(){
          this.planV2Draft={items:{}, edges:{}};
          this.planV2History=[]; this.planV2Future=[];
        },

      _planV2EffectiveDims(plan){
          // §7.2: у floor — размеры изображения, у single_line без картинки —
          // логический холст (canvas_width/height). Та же логика, что
          // PlanV2.effective_width/height в plan_service_v2.py.
          if(plan.image_width && plan.image_height) return [plan.image_width, plan.image_height];
          return [plan.canvas_width||2000, plan.canvas_height||1200];
        },

      _planV2XyToLatLng(x, y, height){
          return [height - y, x];
        },

      planV2ItemColor(kind){ return _planV2KindColor(kind); },

      planV2PendingCount(){
          return Object.keys(this.planV2Draft.items).length + Object.keys(this.planV2Draft.edges).length;
        },

      planV2NodeLikeItems(){
          return this._planV2EffectiveItems().filter(it=>it.kind==='node'||it.kind==='port');
        },

      _planV2EffectiveItems(){
          if(!this.planV2Current) return [];
          const draft=this.planV2Draft.items;
          const out=[];
          (this.planV2Current.items||[]).forEach(it=>{
            const rec=draft[String(it.id)];
            if(rec && rec.op==='remove') return;
            out.push(rec && rec.op==='upsert' ? {...it, ...rec, id:it.id} : it);
          });
          Object.keys(draft).forEach(key=>{
            if(!key.startsWith('new:')) return;
            const rec=draft[key];
            if(rec.op==='upsert') out.push({...rec, id:key});
          });
          return out;
        },

      _planV2EffectiveEdgeViews(){
          if(!this.planV2Current) return [];
          const draft=this.planV2Draft.edges;
          const out=[];
          (this.planV2Current.edges||[]).forEach(v=>{
            const rec=draft[String(v.id)];
            if(rec && rec.op==='remove') return;
            out.push(rec && rec.op==='upsert' ? {...v, ...rec, id:v.id} : v);
          });
          Object.keys(draft).forEach(key=>{
            if(!key.startsWith('newedge:')) return;
            const rec=draft[key];
            if(rec.op==='upsert') out.push({...rec, id:key});
          });
          return out;
        },

      _planV2PushHistory(){
          this.planV2History.push(JSON.parse(JSON.stringify(this.planV2Draft)));
          if(this.planV2History.length>50) this.planV2History.shift();
          this.planV2Future=[];
        },

      planV2Undo(){
          if(!this.planV2History.length) return;
          this.planV2Future.push(JSON.parse(JSON.stringify(this.planV2Draft)));
          this.planV2Draft=this.planV2History.pop();
          this.planV2SelectedItem=null; this.planV2SelectedEdgeView=null;
          this._planV2RenderItems();
        },

      planV2Redo(){
          if(!this.planV2Future.length) return;
          this.planV2History.push(JSON.parse(JSON.stringify(this.planV2Draft)));
          this.planV2Draft=this.planV2Future.pop();
          this.planV2SelectedItem=null; this.planV2SelectedEdgeView=null;
          this._planV2RenderItems();
        },

      planV2RequestDiscard(){
          if(this.planV2PendingCount()===0) return;
          this.planV2ConfirmDiscard=true;
        },

      planV2ConfirmDiscardNow(){
          this.planV2ConfirmDiscard=false;
          this._planV2ResetDraft();
          this.planV2SelectedItem=null; this.planV2SelectedEdgeView=null;
          this._planV2RenderItems();
        },

      togglePlanV2Edit(){
          this.planV2EditMode=!this.planV2EditMode;
          this.planV2Placing=false;
          if(this.planV2EditMode && !this.planV2PickersLoaded){
            this.planV2PickersLoaded=true;
            this._planV2LoadPickers();
          }
          this._planV2RenderItems();
        },

      async _planV2LoadPickers(){
          try{
            const [pts, locs, nodes, edgesDraft, edgesPub] = await Promise.all([
              fetch('/api/v2/points').then(r=>r.json()),
              fetch('/api/v2/locations').then(r=>r.json()),
              fetch('/api/v2/topology/nodes').then(r=>r.json()),
              fetch('/api/v2/topology/edges?state=draft').then(r=>r.json()),
              fetch('/api/v2/topology/edges?state=published').then(r=>r.json()),
            ]);
            this.planV2Points=Array.isArray(pts)?pts:[];
            this.planV2Locations=Array.isArray(locs)?locs:[];
            this.planV2Nodes=Array.isArray(nodes)?nodes:[];
            const byId={};
            [...(Array.isArray(edgesPub)?edgesPub:[]), ...(Array.isArray(edgesDraft)?edgesDraft:[])]
              .forEach(e=>{ byId[e.id]=e; });
            this.planV2Edges=Object.values(byId);
          }catch(e){ console.error('planV2: не удалось загрузить справочники для редактирования', e); }
        },

      _planV2BaseItemFields(effItem){
          return {kind:effItem.kind, point_id:effItem.point_id ?? null,
                  location_id:effItem.location_id ?? null, group_id:effItem.group_id ?? null,
                  node_id:effItem.node_id ?? null, target_plan_id:effItem.target_plan_id ?? null,
                  label:effItem.label ?? null};
        },

      _planV2LatLngToXy(latlng, height){
          return [latlng.lng, height-latlng.lat];
        },

      _planV2OnItemDragEnd(effItem, latlng, height){
          this._planV2PushHistory();
          const key=String(effItem.id);
          const base=this.planV2Draft.items[key] && this.planV2Draft.items[key].op==='upsert'
            ? this.planV2Draft.items[key] : this._planV2BaseItemFields(effItem);
          const [x,y]=this._planV2LatLngToXy(latlng, height);
          this.planV2Draft.items[key]={...base, op:'upsert',
            geometry:{x,y}, coord_space:effItem.coord_space};
          this._planV2RenderItems();
          // Партия 6, задача 2 (§3): перетащили метку точки внутрь контура
          // места — предложить (с подтверждением) отнести точку к этому месту.
          if(effItem.kind==='point' && effItem.point_id!=null){
            this._planV2ProposePointLocationIfInside({point_id:effItem.point_id, geometry:{x,y}});
          }
        },

      planV2RemoveItem(effItem){
          if(!effItem) return;
          this._planV2PushHistory();
          const key=String(effItem.id);
          if(typeof effItem.id==='string' && effItem.id.startsWith('new:')){
            delete this.planV2Draft.items[key];
          }else{
            this.planV2Draft.items[key]={op:'remove'};
          }
          // Связи этого же черновика, ссылающиеся на убираемый элемент, теряют
          // смысл — убираем их из черновика тоже (уже сохранённые edge_views
          // сервер сам отвяжет по ON DELETE SET NULL при реальном удалении).
          Object.entries(this.planV2Draft.edges).forEach(([ek,erec])=>{
            if(erec.op==='upsert' && (erec.from_item_id===effItem.id || erec.to_item_id===effItem.id)){
              delete this.planV2Draft.edges[ek];
            }
          });
          if(this.planV2SelectedItem && this.planV2SelectedItem.id===effItem.id) this.planV2SelectedItem=null;
          this._planV2RenderItems();
        },

      planV2StartPlace(){
          if(!this.planV2AddKind) return;
          if(['point','location','node','port'].includes(this.planV2AddKind) && !this.planV2AddRefId){
            this.showToast('Выберите объект для размещения', false); return;
          }
          this.planV2Placing=true;
        },

      planV2CancelPlace(){ this.planV2Placing=false; },

      _planV2OnMapClick(e){
          // Пока идёт рисование линии edge_view (leaflet-geoman) — клики по
          // карте принадлежат geoman (расстановка вершин линии), а не плейсменту
          // новых item; иначе один и тот же клик ставил бы ещё и метку.
          if(this.planV2DrawingEdgeKey!==null) return;
          if(!this.planV2Placing || !this.planV2Current) return;
          const [W,H]=this._planV2EffectiveDims(this.planV2Current);
          const [x,y]=this._planV2LatLngToXy(e.latlng, H);
          if(x<0||x>W||y<0||y>H){ this.showToast('Точка вне границ плана', false); return; }
          const coordSpace=this.planV2Current.plan_kind==='floor' ? 'image_px_xy_v2' : 'canvas_xy_v2';
          this._planV2PushHistory();
          const key='new:'+(++this.planV2NewItemSeq);
          const rec={op:'upsert', kind:this.planV2AddKind, geometry:{x,y}, coord_space:coordSpace,
                     label:this.planV2AddLabel||null, point_id:null, location_id:null,
                     group_id:null, node_id:null, target_plan_id:null};
          if(this.planV2AddKind==='point') rec.point_id=this.planV2AddRefId;
          if(this.planV2AddKind==='location') rec.location_id=this.planV2AddRefId;
          if(this.planV2AddKind==='node') rec.node_id=this.planV2AddRefId;
          if(this.planV2AddKind==='port'){
            rec.node_id=this.planV2AddRefId;
            if(this.planV2AddTargetPlanId) rec.target_plan_id=this.planV2AddTargetPlanId;
          }
          this.planV2Draft.items[key]=rec;
          this.planV2Placing=false;
          this.planV2AddKind=''; this.planV2AddRefId=''; this.planV2AddLabel=''; this.planV2AddTargetPlanId='';
          this._planV2RenderItems();
          this.showToast('Элемент добавлен в черновик', true);
          // Партия 6, задача 2 (§3): поставили метку точки внутрь контура
          // места — предложить (с подтверждением) отнести точку к этому месту.
          if(rec.kind==='point' && rec.point_id!=null){
            this._planV2ProposePointLocationIfInside({point_id:rec.point_id, geometry:{x,y}});
          }
        },

      planV2ToggleAddingEdgeView(){
          this.planV2AddingEdgeView=!this.planV2AddingEdgeView;
          this.planV2EdgeForm={edgeId:'', fromItemKey:'', toItemKey:''};
        },

      _planV2ParseItemKey(key){
          if(!key) return null;
          return key.startsWith('new:') ? key : Number(key);
        },

      planV2SaveEdgeDraft(){
          const f=this.planV2EdgeForm;
          if(!f.edgeId){ this.showToast('Выберите связь (edge)', false); return; }
          this._planV2PushHistory();
          const key='newedge:'+(++this.planV2NewEdgeSeq);
          this.planV2Draft.edges[key]={
            op:'upsert', edge_id:Number(f.edgeId),
            from_item_id:this._planV2ParseItemKey(f.fromItemKey),
            to_item_id:this._planV2ParseItemKey(f.toItemKey),
            view_kind:'structural', waypoints:null,
          };
          this.planV2AddingEdgeView=false;
          this._planV2RenderItems();
          this.showToast('Связь добавлена в черновик', true);
        },

      planV2RemoveEdgeView(view){
          if(!view) return;
          this._planV2PushHistory();
          const key=String(view.id);
          if(typeof view.id==='string' && view.id.startsWith('newedge:')){
            delete this.planV2Draft.edges[key];
          }else{
            this.planV2Draft.edges[key]={op:'remove'};
          }
          if(this.planV2SelectedEdgeView && this.planV2SelectedEdgeView.id===view.id) this.planV2SelectedEdgeView=null;
          this._planV2RenderItems();
        },

      _planV2BuildLayoutPayload(){
          const itemOps=[]; const idxByKey={};
          Object.entries(this.planV2Draft.items).forEach(([key,rec])=>{
            if(rec.op==='remove'){ itemOps.push({op:'remove', id:Number(key)}); return; }
            const isNew=key.startsWith('new:');
            const entry={op:'upsert', geometry:rec.geometry, coord_space:rec.coord_space,
                         label:rec.label ?? null};
            if(!isNew){ entry.id=Number(key); }
            else{
              entry.kind=rec.kind;
              if(rec.point_id!=null) entry.point_id=rec.point_id;
              if(rec.location_id!=null) entry.location_id=rec.location_id;
              if(rec.group_id!=null) entry.group_id=rec.group_id;
              if(rec.node_id!=null) entry.node_id=rec.node_id;
              if(rec.target_plan_id!=null) entry.target_plan_id=rec.target_plan_id;
            }
            idxByKey[key]=itemOps.length;
            itemOps.push(entry);
          });
          const resolveRef=(ref)=>{
            if(ref==null) return null;
            if(typeof ref==='string' && ref.startsWith('new:')){
              if(!(ref in idxByKey)) throw new Error('Элемент для связи ещё не размещён в этом черновике');
              return '$'+idxByKey[ref];
            }
            return Number(ref);
          };
          const edgeViewOps=[];
          Object.entries(this.planV2Draft.edges).forEach(([key,rec])=>{
            if(rec.op==='remove'){ edgeViewOps.push({op:'remove', id:Number(key)}); return; }
            const entry={op:'upsert', edge_id:rec.edge_id,
                         from_item_id:resolveRef(rec.from_item_id), to_item_id:resolveRef(rec.to_item_id),
                         waypoints:rec.waypoints ?? null, view_kind:rec.view_kind || 'structural'};
            if(!key.startsWith('newedge:')) entry.id=Number(key);
            edgeViewOps.push(entry);
          });
          return {expected_revision:this.planV2Current.canvas_revision,
                  item_ops:itemOps, edge_view_ops:edgeViewOps};
        },

      async planV2SaveLayout(){
          if(!this.planV2Current || this.planV2PendingCount()===0) return;
          let payload;
          try{ payload=this._planV2BuildLayoutPayload(); }
          catch(e){ this.showToast(String(e.message||e), false); return; }
          this.planV2Saving=true;
          try{
            const r=await fetch('/api/v2/plans/'+this.planV2Current.id+'/layout', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body:JSON.stringify(payload),
            });
            if(r.status===409){
              this.planV2RevisionConflict=true;
              this.planV2Saving=false;
              return;
            }
            if(!r.ok){
              const err=await r.json().catch(()=>({}));
              this.showToast('Ошибка сохранения: '+(err.error && err.error.message || r.status), false);
              this.planV2Saving=false;
              return;
            }
            this._planV2ResetDraft();
            this.planV2SelectedItem=null; this.planV2SelectedEdgeView=null;
            await this.selectPlanV2(this.planV2Current.id);
            this.showToast('Изменения плана сохранены', true);
          }catch(e){
            this.showToast('Ошибка сети при сохранении: '+e, false);
          }
          this.planV2Saving=false;
        },

      planV2ConfirmDiscardReload(){
          this.planV2RevisionConflict=false;
          const id=this.planV2Current.id;
          this._planV2ResetDraft();
          this.selectPlanV2(id);
        },

      _planV2BaseEdgeFields(view){
          return {edge_id:view.edge_id, from_item_id:view.from_item_id ?? null,
                  to_item_id:view.to_item_id ?? null, view_kind:view.view_kind || 'structural',
                  waypoints:view.waypoints ?? null};
        },

      _planV2CleanupDrawLayer(){
          if(_planV2DrawLayer){ try{_planV2Map.removeLayer(_planV2DrawLayer);}catch(e){} _planV2DrawLayer=null; }
        },

      planV2StartDrawWaypoints(view){
          if(!view || this.planV2DrawingEdgeKey!==null || !_planV2Map || !this.planV2EditMode) return;
          this.planV2DrawingEdgeKey=String(view.id);
          this.planV2HasPendingEdgeDraw=false;
          // Баг из браузера (§5, "рисование линии никогда не завершается") — та
          // же причина и то же лечение, что у startZoneDraw() выше: Leaflet
          // doubleClickZoom забирает второй клик двойного клика себе (зумит
          // карту), geoman finishOn:'dblclick' может не увидеть событие.
          // Единственные выходы из режима — planV2SaveDrawnWaypoints() и
          // planV2CancelDrawWaypoints() (включая по Escape, см. init()) — оба
          // возвращают doubleClickZoom обратно.
          if(_planV2Map.doubleClickZoom) _planV2Map.doubleClickZoom.disable();
          _planV2Map.pm.enableDraw('Line', {snappable:true, finishOn:'dblclick'});
        },

      _planV2OnPmCreate(e){
          if(e.shape==='Line' && this.planV2DrawingEdgeKey!==null){
            if(_planV2DrawLayer){ try{_planV2Map.removeLayer(_planV2DrawLayer);}catch(err){} }
            _planV2DrawLayer=e.layer;
            if(_planV2DrawLayer.pm) _planV2DrawLayer.pm.enable({allowSelfIntersection:false});
            this.planV2HasPendingEdgeDraw=true;
          }else if(e.shape==='Polygon' && this.planV2PlacingPolygon){
            // Партия 6, задача 2 (§3): контур места на плане v2 — рисуется тем
            // же geoman-инструментом Polygon, что и legacy-зоны (startZoneDraw
            // выше), но здесь коммитится в черновик сразу по завершении рисования
            // (тот же стиль, что и одиночная точка в _planV2OnMapClick), а не
            // ждёт отдельной кнопки "Сохранить" — контур редактировать вершины
            // после рисования незачем, у него нет отдельного шага правки.
            const rings=e.layer.getLatLngs();
            const ring=Array.isArray(rings[0]) ? rings[0] : rings;
            try{_planV2Map.removeLayer(e.layer);}catch(err){}
            if(_planV2Map && _planV2Map.pm) _planV2Map.pm.disableDraw('Polygon');
            if(_planV2Map && _planV2Map.doubleClickZoom) _planV2Map.doubleClickZoom.enable();
            this.planV2PlacingPolygon=false;
            if(ring.length<3){ this.showToast('Контур должен содержать минимум 3 точки', false); return; }
            const [W,H]=this._planV2EffectiveDims(this.planV2Current);
            const coordSpace=this.planV2Current.plan_kind==='floor' ? 'image_px_xy_v2' : 'canvas_xy_v2';
            const geometry=ring.map(ll=>this._planV2LatLngToXy(ll, H));
            this._planV2PushHistory();
            const key='new:'+(++this.planV2NewItemSeq);
            this.planV2Draft.items[key]={op:'upsert', kind:'location', geometry, coord_space:coordSpace,
              label:this.planV2AddLabel||null, point_id:null, location_id:this.planV2AddRefId,
              group_id:null, node_id:null, target_plan_id:null};
            this.planV2AddKind=''; this.planV2AddRefId=''; this.planV2AddLabel='';
            this._planV2RenderItems();
            this.showToast('Контур места добавлен в черновик', true);
            // Партия 6, задача 2 (§3): марш нанесённых точек может теперь
            // попадать внутрь этого нового контура — предложим (с явным
            // подтверждением, никогда не молча) отнести их к месту.
            this._planV2ProposeAssignForPointsInPolygon(key);
          }
        },

      _planV2PointInPolygon(x, y, ring){
          let inside=false;
          for(let i=0,j=ring.length-1;i<ring.length;j=i++){
            const xi=ring[i][0], yi=ring[i][1], xj=ring[j][0], yj=ring[j][1];
            const intersect=((yi>y)!==(yj>y)) && (x < (xj-xi)*(y-yi)/(yj-yi)+xi);
            if(intersect) inside=!inside;
          }
          return inside;
        },

      _planV2FindContainingLocation(x, y){
          const items=this._planV2EffectiveItems().filter(it=>
            it.kind==='location' && Array.isArray(it.geometry) && it.geometry.length>=3 && it.location_id!=null);
          return items.find(it=>this._planV2PointInPolygon(x, y, it.geometry)) || null;
        },

      _planV2ProposePointLocationIfInside(pointRef){
          const loc=this._planV2FindContainingLocation(pointRef.geometry.x, pointRef.geometry.y);
          if(!loc) return;
          const structPoint=(this.structPoints||[]).find(p=>p.point_id===pointRef.point_id);
          if(structPoint && structPoint.location_id===loc.location_id) return; // уже так и есть — предлагать нечего
          const planPoint=(this.planV2Points||[]).find(p=>p.id===pointRef.point_id);
          const planLoc=(this.planV2Locations||[]).find(l=>l.id===loc.location_id);
          this.planV2LocationAssignProposal={
            pointId: pointRef.point_id,
            pointName: (planPoint&&planPoint.name) || (structPoint&&structPoint.name) || ('#'+pointRef.point_id),
            locationId: loc.location_id,
            locationName: (planLoc&&planLoc.name) || ('#'+loc.location_id),
          };
        },

      _planV2ProposeAssignForPointsInPolygon(polygonItemKey){
          const polyItem=this.planV2Draft.items[polygonItemKey];
          if(!polyItem || !Array.isArray(polyItem.geometry)) return;
          const items=this._planV2EffectiveItems().filter(it=>
            it.kind==='point' && it.point_id!=null && it.geometry && typeof it.geometry.x==='number');
          const hit=items.find(it=>this._planV2PointInPolygon(it.geometry.x, it.geometry.y, polyItem.geometry));
          // Один диалог за раз — не заваливаем пользователя вопросами подряд,
          // если контур случайно охватил сразу несколько уже нанесённых точек.
          if(hit) this._planV2ProposePointLocationIfInside(hit);
        },

      async planV2ConfirmLocationAssign(){
          const p=this.planV2LocationAssignProposal;
          if(!p) return;
          try{
            const rev=await this._structCurrentRev();
            const r=await fetch('/api/v2/points/'+p.pointId, {
              method:'PATCH', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({installation_location_id:p.locationId, expected_revision:rev})});
            const d=await r.json();
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); this.planV2LocationAssignProposal=null; return; }
            this.showToast('Место точки обновлено', true);
            await this._loadStructureLists();
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
          this.planV2LocationAssignProposal=null;
        },

      planV2DeclineLocationAssign(){ this.planV2LocationAssignProposal=null; },

      planV2StartPlacePolygon(){
          if(this.planV2AddKind!=='location' || !this.planV2AddRefId){
            this.showToast('Выберите место для размещения', false); return;
          }
          if(!_planV2Map) return;
          this.planV2PlacingPolygon=true;
          if(_planV2Map.doubleClickZoom) _planV2Map.doubleClickZoom.disable();
          _planV2Map.pm.enableDraw('Polygon', {snappable:true, finishOn:'dblclick'});
        },

      planV2CancelPlacePolygon(){
          if(_planV2Map && _planV2Map.pm) _planV2Map.pm.disableDraw('Polygon');
          if(_planV2Map && _planV2Map.doubleClickZoom) _planV2Map.doubleClickZoom.enable();
          this.planV2PlacingPolygon=false;
        },

      planV2SaveDrawnWaypoints(){
          if(!_planV2DrawLayer || this.planV2DrawingEdgeKey===null || !this.planV2Current) return;
          const key=this.planV2DrawingEdgeKey;
          const view=this._planV2EffectiveEdgeViews().find(v=>String(v.id)===key);
          if(!view){ this.planV2CancelDrawWaypoints(); return; }
          const latlngs=_planV2DrawLayer.getLatLngs();
          if(latlngs.length<2){ this.showToast('Линия должна иметь минимум 2 точки', false); return; }
          const [,H]=this._planV2EffectiveDims(this.planV2Current);
          const waypoints=latlngs.map(ll=>this._planV2LatLngToXy(ll, H));
          this._planV2PushHistory();
          const base=this.planV2Draft.edges[key] && this.planV2Draft.edges[key].op==='upsert'
            ? this.planV2Draft.edges[key] : this._planV2BaseEdgeFields(view);
          this.planV2Draft.edges[key]={...base, op:'upsert', waypoints};
          if(_planV2Map && _planV2Map.pm) _planV2Map.pm.disableDraw('Line');
          if(_planV2Map && _planV2Map.doubleClickZoom) _planV2Map.doubleClickZoom.enable();
          this._planV2CleanupDrawLayer();
          this.planV2DrawingEdgeKey=null; this.planV2HasPendingEdgeDraw=false;
          this._planV2RenderItems();
          this.showToast('Линия связи обновлена в черновике', true);
        },

      planV2CancelDrawWaypoints(){
          if(_planV2Map && _planV2Map.pm) _planV2Map.pm.disableDraw('Line');
          if(_planV2Map && _planV2Map.doubleClickZoom) _planV2Map.doubleClickZoom.enable();
          this._planV2CleanupDrawLayer();
          this.planV2DrawingEdgeKey=null;
          this.planV2HasPendingEdgeDraw=false;
        },

      planV2ClearWaypoints(view){
          if(!view || !view.waypoints) return;
          this._planV2PushHistory();
          const key=String(view.id);
          const base=this.planV2Draft.edges[key] && this.planV2Draft.edges[key].op==='upsert'
            ? this.planV2Draft.edges[key] : this._planV2BaseEdgeFields(view);
          this.planV2Draft.edges[key]={...base, op:'upsert', waypoints:null};
          this._planV2RenderItems();
        },

      _planV2ItemLayerFor(item, height){
          if(item.coord_space!=='image_px_xy_v2' && item.coord_space!=='canvas_xy_v2'){
            console.warn('planV2: неподдерживаемый coord_space на элементе', item);
            return null;
          }
          const g=item.geometry;
          // Партия 6, задача 2 (§3): место может быть контуром-полигоном
          // ([[x,y],...] — plan_geo_v2.py, kind='location'), а не только точкой.
          // Полигон не перетаскивается как целое (в отличие от точечных меток
          // ниже) — тот же уровень возможностей, что у legacy-зон на v1-плане:
          // чтобы переместить контур, его убирают и рисуют заново.
          if(item.kind==='location' && Array.isArray(g)){
            if(g.length<3) return null;
            const latlngs=g.map(([x,y])=>this._planV2XyToLatLng(x,y,height));
            const color=_planV2KindColor(item.kind);
            const layer=L.polygon(latlngs, {color, fillColor:color, fillOpacity:.25, weight:2});
            layer.on('click', (e)=>{
              if(e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
              this.planV2SelectedEdgeView=null;
              this.planV2SelectedItem=item;
            });
            return layer;
          }
          if(!g || typeof g.x!=='number' || typeof g.y!=='number') return null;
          const latlng=this._planV2XyToLatLng(g.x, g.y, height);
          const color=_planV2KindColor(item.kind);
          const layer=L.circleMarker(latlng, {radius:8, color, fillColor:color, fillOpacity:.7, weight:2});
          layer.on('click', (e)=>{
            if(e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
            this.planV2SelectedEdgeView=null;
            this.planV2SelectedItem=item;
          });
          // Перетаскивание — через leaflet-geoman (см. комментарий у секции
          // "План v2" выше): L.circleMarker не умеет draggable:true нативно.
          if(this.planV2EditMode && layer.pm){
            layer.pm.enableLayerDrag();
            layer.on('pm:dragend', ()=>{
              this._planV2OnItemDragEnd(item, layer.getLatLng(), height);
            });
          }
          return layer;
        },

      _planV2RenderItems(){
          if(!_planV2Map || !this.planV2Current) return;
          _planV2ItemLayers.forEach(layer=>{ try{_planV2Map.removeLayer(layer);}catch(e){} });
          _planV2ItemLayers.clear();
          _planV2EdgeLayers.forEach(layer=>{ try{_planV2Map.removeLayer(layer);}catch(e){} });
          _planV2EdgeLayers.clear();
      
          const [,H]=this._planV2EffectiveDims(this.planV2Current);
          const items=this._planV2EffectiveItems();
          const itemById={};
          items.forEach(it=>{
            itemById[String(it.id)]=it;
            const layer=this._planV2ItemLayerFor(it, H);
            if(layer){ layer.addTo(_planV2Map); _planV2ItemLayers.set(String(it.id), layer); }
          });
      
          this._planV2EffectiveEdgeViews().forEach(v=>{
            let latlngs=[];
            if(v.waypoints && v.waypoints.length){
              latlngs=v.waypoints.map(([x,y])=>this._planV2XyToLatLng(x,y,H));
            }else{
              const fromIt=v.from_item_id!=null ? itemById[String(v.from_item_id)] : null;
              const toIt=v.to_item_id!=null ? itemById[String(v.to_item_id)] : null;
              if(fromIt && fromIt.geometry && toIt && toIt.geometry){
                latlngs=[this._planV2XyToLatLng(fromIt.geometry.x, fromIt.geometry.y, H),
                         this._planV2XyToLatLng(toIt.geometry.x, toIt.geometry.y, H)];
              }
            }
            if(latlngs.length<2) return;
            const line=L.polyline(latlngs, {color:'#e0995f', weight:3,
                                             dashArray:v.view_kind==='cable_route'?null:'6,4'});
            line.on('click', (e)=>{
              if(e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
              this._planV2SelectEdgeView(v);
            });
            line.addTo(_planV2Map);
            _planV2EdgeLayers.set(String(v.id), line);
          });
        },

      _planV2RenderBase(){
          if(!_planV2Map || !this.planV2Current) return;
          if(_planV2ImageLayer){ try{_planV2Map.removeLayer(_planV2ImageLayer);}catch(e){} _planV2ImageLayer=null; }
      
          const pc=this.planV2Current;
          const [W,H]=this._planV2EffectiveDims(pc);
          const bounds=[[0,0],[H,W]];
          if(pc.plan_kind==='floor'){
            _planV2ImageLayer=L.imageOverlay('/api/v2/plans/'+pc.id+'/image', bounds).addTo(_planV2Map);
          }else{
            _planV2ImageLayer=L.rectangle(bounds, {color:_planCssVar('--line'), weight:1, fill:false, dashArray:'4,4'}).addTo(_planV2Map);
          }
          _planV2Map.fitBounds(bounds);
          this._planV2RenderItems();
        },

      planV2ItemKindLabel(kind){
          const labels={point:'Точка учёта', location:'Место', group:'Группа',
                        node:'Узел сети', port:'Переход на другой план', annotation:'Подпись'};
          return labels[kind]||kind;
        },

      planV2ItemInspectorTarget(item){
          if(!item) return null;
          if(item.kind==='point' && item.point_id!=null) return {kind:'point', id:item.point_id};
          if(item.kind==='node' && item.node_id!=null) return {kind:'node', id:item.node_id};
          if(item.kind==='group' && item.group_id!=null) return {kind:'group', id:item.group_id};
          return null;
        }
    };
  });
})();

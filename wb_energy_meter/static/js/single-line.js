// single-line.js — однолинейная схема на «Плане v3» (партия 14, docs/design-single-line.md).
//
// Схема — это план plan_kind='single_line': те же узлы и линии, что на плане этажа,
// только как представление (plan_items / plan_edge_views). Здесь: значки и подписи
// узлов, ортогональные трассы линий, P и загрузка линии, баланс щита, автораскладка
// (чистая часть — scheme-layout.js), ручная трасса, создание схемы и печать.
// «План v3» (plan-v3.js) вызывает этот файл только для планов вида single_line;
// план этажа работает как раньше.
//
// Объекты Leaflet держим ВНЕ реактивности Alpine (как _planV3Map в plan-v3.js).
// Периодический опрос — только через общий _pollTick (ветка planv3 → _planV3PollLiveCard
// → _slPollScheme): ни одного нового таймера.
const _slLabelLayers=new Map();   // id вида линии → L.marker с подписью (имя · P · загрузка)
let _slRouteCtx=null;             // {pos, edges, H, layers} последней отрисовки (для drag)
let _slBounds=null;               // границы холста схемы (печать)
let _slPrintSaved=null;

// Цвет вида — исторические значения (по ним тесты «План v3» отличают вид узла); значок рисуется
// токенами темы (.sl-ico.k-*), чтобы читаться и в светлой, и в тёмной теме.
const SL_KIND_COLOR={source:'#4fa8f5', panel:'#3dd6c0', junction:'#9aa0a8', load:'#f0a83c'};
const SL_SVG={
  source:'<circle cx="12" cy="12" r="8"/><path d="M7 12c1.7-3.6 3.3-3.6 5 0s3.3 3.6 5 0"/>',
  panel:'<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9.5h8M8 12.5h8M8 15.5h8"/>',
  junction:'<circle cx="12" cy="12" r="3" fill="currentColor"/><path d="M12 4v5M12 15v5M4 12h5"/>',
  load:'<path d="M12 4v8"/><path d="M7 12l5 7 5-7z" fill="currentColor"/>',
};

// Печать: перед печатью карте задаётся размер листа и она вписывается заново; после — как было.
window.addEventListener('beforeprint', ()=>{
  if(!_planV3Map || document.documentElement.getAttribute('data-sl')!=='1' || !_slBounds) return;
  const el=_planV3Map.getContainer();
  _slPrintSaved={w:el.style.width, h:el.style.height};
  el.style.width='980px'; el.style.height='680px';
  _planV3Map.invalidateSize({animate:false});
  _planV3Map.fitBounds(_slBounds, {animate:false});
});
window.addEventListener('afterprint', ()=>{
  if(!_planV3Map || !_slPrintSaved) return;
  const el=_planV3Map.getContainer();
  el.style.width=_slPrintSaved.w; el.style.height=_slPrintSaved.h; _slPrintSaved=null;
  _planV3Map.invalidateSize({animate:false});
  if(_slBounds) _planV3Map.fitBounds(_slBounds, {animate:false});
});

(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function singleLinePart() {
    return {
      slAutoBusy:false,
      // {planId, moved:[{id, geometry, coord_space, label, sort_order}], itemIds:[], viewIds:[]}
      slUndo:null,
      // 409 при сохранении раскладки: окно конфликта; расчёт остаётся в slPendingMode
      slConflictOpen:false,
      slPendingMode:null,
      // edge_id → ответ GET /api/v2/topology/edge-loads; node_id → строка баланса щита
      slLoads:{},
      slBalance:{},
      slBalanceAt:0,
      slDrawingEdgeId:null,
      slNewSchemeOpen:false,
      slNewSchemeName:'',
      slNewSchemeBusy:false,

      planV3IsScheme(){
          return !!(this.planV3Current && this.planV3Current.plan_kind==='single_line');
        },

      // Сколько узлов схемы ещё не стоит на текущей схеме.
      slUnplacedCount(){
          return (this.structNodes||[]).filter(n=>!this.planV3NodePlaced(n.id)).length;
        },

      // ---------------- переключатель вида планов ----------------
      slPlansOfKind(kind){
          return (this.planV3Plans||[]).filter(p=>(p.plan_kind==='single_line')===(kind==='single_line'));
        },

      // ---------------- новая схема: холст по размеру топологии ----------------
      _slLabelOf(n){
          const mc=this.structNodeMeterCount ? this.structNodeMeterCount(n.id) : 0;
          return (n.name||n.code||'')+(mc===0?'':(mc===1?' · счётчик':(' · счётчиков: '+mc)));
        },

      _slLayoutInput(canvas, mode, fixed){
          const nodes=(this.structNodes||[]).map(n=>({id:n.id, kind:n.kind, label:this._slLabelOf(n)}));
          const edges=(this.structEdges||[]).map(e=>({id:e.id, from:e.from_node_id, to:e.to_node_id}));
          return {nodes, edges, canvas, mode, fixed:fixed||{}};
        },

      async slCreateScheme(){
          if(this.slNewSchemeBusy) return;
          const name=(this.slNewSchemeName||'').trim() || 'Однолинейная схема';
          this.slNewSchemeBusy=true;
          try{
            if(!this.structInited){ this.structInited=true; await this._loadStructureLists(); }
            // Холст не меняется после создания плана, поэтому размер берём по топологии:
            // нужный размер раскладки + 5% запаса, кратно 100, не меньше 1200×700. Небольшая
            // схема тогда вписывается в экран почти в натуральную величину (подписи читаются).
            const big={width:1000000, height:1000000};
            const probe=SchemeLayout.layoutTree(this._slLayoutInput(big, 'all'));
            const up=(v)=>Math.ceil(v*1.05/100)*100;
            const w=Math.max(1200, up(probe.needed.width)), h=Math.max(700, up(probe.needed.height));
            const fd=new FormData();
            fd.append('name', name); fd.append('plan_kind', 'single_line');
            fd.append('canvas_width', String(w)); fd.append('canvas_height', String(h));
            const r=await fetch('/api/v2/plans', {method:'POST', body:fd});
            const d=await r.json();
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            this.slNewSchemeName=''; this.slNewSchemeOpen=false;
            await this.loadPlanV3List();
            await this.selectPlanV3(d.id);
            this.showToast('Схема «'+name+'» создана ('+w+'×'+h+'). Нажмите «Разложить неразмещённые».', true);
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
          finally{ this.slNewSchemeBusy=false; }
        },

      // Подпись вида для читалки экрана: словарь NODE_KINDS (core.js) — единственный источник.
      _slKindLabel(kind){
          const k=NODE_KINDS.find(x=>x.value===kind);
          return k ? k.label : 'Элемент';
        },

      // ---------------- значки и подписи ----------------
      _slBalanceHtml(nodeId){
          const b=this.slBalance[nodeId];
          if(!b) return '';
          // Скрытый разделитель — чтобы текст подсказки читался как «Щит 1 · Δ …» (как «Щит 1 · счётчик»).
          return '<span class="sl-sep" aria-hidden="true"> · </span>'
            +'<span class="sl-bal '+b.cls+'" data-sl-balance="'+_planEscapeHtml(String(b.value))+'">'
            +_planEscapeHtml(b.text)+'</span>';
        },

      _slTipHtml(node, item){
          const base=_planEscapeHtml(this._slLabelOf(node));
          const bal=node.kind==='panel' ? this._slBalanceHtml(node.id) : '';
          return base+bal;
        },

      _slNodeLayer(item, node, height){
          const kind=(node && SL_SVG[node.kind]) ? node.kind : 'panel';
          const nm=node ? (node.name||node.code||'') : '';
          const html='<div class="sl-ico k-'+kind+'" role="img" aria-label="'
            +_planEscapeHtml(this._slKindLabel(kind)+': '+nm)+'">'
            +'<svg viewBox="0 0 24 24" aria-hidden="true">'+SL_SVG[kind]+'</svg></div>';
          const icon=L.divIcon({className:'sl-node', html, iconSize:[36,36], iconAnchor:[18,18],
                                tooltipAnchor:[18,0]});
          const latlng=this._planV3XyToLatLng(item.geometry.x, item.geometry.y, height);
          const layer=L.marker(latlng, {icon, draggable:true, keyboard:false, autoPan:false,
                                        color:SL_KIND_COLOR[kind]});
          if(node){
            const mc=this.structNodeMeterCount(node.id);
            layer.bindTooltip(this._slTipHtml(node, item),
              {permanent:true, direction:'right', interactive:false,
               className:'planv3-node-tip sl-node-tip'+(mc>0?' planv3-has-meter':'')});
          }
          layer.on('click', (e)=>{
            // Пока рисуется трасса, клик по значку — клик по карте для Geoman (не открываем карточку)
            if(this.slDrawingEdgeId!=null) return;
            if(e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
            this._planV3OnNodeMarkerClick(item);
          });
          layer.on('drag', ()=>this._slRerouteDuring(item, layer.getLatLng()));
          layer.on('dragend', ()=>{ this._planV3OnItemDragEnd(item, layer.getLatLng(), height); });
          return layer;
        },

      // ---------------- линии ----------------
      _slPowerText(w){
          if(w==null) return '';
          return Math.abs(w)>=1000 ? (w/1000).toFixed(2)+' кВт' : Math.round(w)+' Вт';
        },

      // Имя линии для людей: своё имя, иначе «откуда → куда» (служебный код линии не показываем).
      _slEdgeTitle(edge){
          if(!edge) return '';
          return edge.name || (this.structNodeName(edge.from_node_id)+' → '+this.structNodeName(edge.to_node_id));
        },

      // Подпись линии в две строки: «имя · счётчик» и «P · загрузка» (или «без счётчика»).
      // Безымянная линия не повторяет «откуда → куда» — концы и так видны на схеме.
      _slLineLabelHtml(v, edge){
          const metered=!!(edge && edge.primary_point_id);
          const ld=edge ? this.slLoads[edge.id] : null;
          const first=[edge && edge.name ? edge.name : '',
                       metered ? this.structPointName(edge.primary_point_id) : ''].filter(Boolean).join(' · ');
          let second;
          if(metered){
            const p=ld ? this._slPowerText(ld.power_w) : '';
            const pct=(ld && ld.load_percent!=null) ? (Math.round(ld.load_percent)+'%') : '';
            second=(p||'P —')+(pct?(' · '+pct):'');
          }else{
            second='без счётчика';
          }
          const full=this._slEdgeTitle(edge)+' — '+(first?first+' · ':'')+second;
          return '<span class="sl-ll'+(metered?' sl-ll-m':'')+'" data-sl-line="'+(edge?edge.id:'')
            +'" title="'+_planEscapeHtml(full)+'">'
            +(first?'<span class="sl-ll-a">'+_planEscapeHtml(first)+'</span>':'')
            +'<span class="sl-ll-b">'+_planEscapeHtml(second)+'</span></span>';
        },

      _slPlaceLabel(viewId, v, edge, points, H){
          const [ax,ay]=SchemeLayout.labelAnchor(points);
          const ll=this._planV3XyToLatLng(ax, ay, H);
          const html=this._slLineLabelHtml(v, edge);
          let m=_slLabelLayers.get(String(viewId));
          if(!m){
            m=L.marker(ll, {icon:L.divIcon({className:'sl-line-label', html, iconSize:null, iconAnchor:[-6,8]}),
                            interactive:false, keyboard:false});
            m._slHtml=html;
            m.addTo(_planV3Map);
            _slLabelLayers.set(String(viewId), m);
          }else{
            m.setLatLng(ll);
            if(m._slHtml!==html){
              m.setIcon(L.divIcon({className:'sl-line-label', html, iconSize:null, iconAnchor:[-6,8]}));
              m._slHtml=html;
            }
          }
        },

      // Отрисовка линий схемы: вместо прямых отрезков «Плана v3» — ортогональные трассы.
      _slRenderEdges(items, H){
          _slLabelLayers.forEach(l=>{ try{_planV3Map.removeLayer(l);}catch(e){} });
          _slLabelLayers.clear();
          const pos={};
          items.forEach(it=>{
            if(it.kind==='node' && it.geometry && typeof it.geometry.x==='number'){
              pos[it.id]={x:it.geometry.x, y:it.geometry.y};
            }
          });
          const views=(this.planV3Current.edges||[]).filter(v=>
            v.from_item_id!=null && v.to_item_id!=null && pos[v.from_item_id] && pos[v.to_item_id]);
          const input=views.map(v=>({id:v.id, from:v.from_item_id, to:v.to_item_id,
            waypoints:(Array.isArray(v.waypoints) && v.waypoints.length>=2) ? v.waypoints : null}));
          const routes=SchemeLayout.routeEdges({items:pos, edges:input}).routes;
          _slRouteCtx={pos, edges:input, H, views};
          // Сплошные (со счётчиком) рисуем поверх пунктирных: на общем участке «шины» виден сплошной.
          const isMetered=(v)=>{
            const e2=(this.structEdges||[]).find(x=>x.id===v.edge_id);
            return !!(e2 && e2.primary_point_id);
          };
          views.slice().sort((a,b)=>Number(isMetered(a))-Number(isMetered(b))).forEach(v=>{
            const route=routes[v.id]; if(!route) return;
            const latlngs=route.points.map(([x,y])=>this._planV3XyToLatLng(x,y,H));
            const edge=(this.structEdges||[]).find(e2=>e2.id===v.edge_id);
            const metered=!!(edge && edge.primary_point_id);
            const color=metered ? _planCssVar('--ok') : '#e0995f';
            const bad=route.manual && !route.orthogonal;
            const line=L.polyline(latlngs, {color, weight:3, dashArray: metered?null:'6,6',
                                            className:'sl-line'+(bad?' sl-line-bad':'')});
            if(edge){
              line.bindTooltip(_planEscapeHtml(this._slEdgeTitle(edge)+(bad?' — трасса не по осям: «Сделать автоматической»':'')),
                               {sticky:true});
            }
            line.on('click', (e)=>{
              if(this.slDrawingEdgeId!=null) return;   // клик по карте для Geoman, не по линии
              if(e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
              if(edge) this.openInspectorInPlace('edge', edge.id);
            });
            line.addTo(_planV3Map);
            _planV3EdgeLayers.set(String(v.id), line);
            this._slPlaceLabel(v.id, v, edge, route.points, H);
          });
        },

      // Перетаскивание значка: линии пересчитываются на лету (автоматические — по новым
      // положениям, ручные тянутся концами); в базу пишет только dragend.
      _slRerouteDuring(item, latlng){
          if(!_slRouteCtx || !_planV3Map) return;
          const H=_slRouteCtx.H;
          const [x,y]=this._planV3LatLngToXy(latlng, H);
          const pos=Object.assign({}, _slRouteCtx.pos);
          pos[item.id]={x,y};
          const routes=SchemeLayout.routeEdges({items:pos, edges:_slRouteCtx.edges}).routes;
          _slRouteCtx.views.forEach(v=>{
            const route=routes[v.id]; const layer=_planV3EdgeLayers.get(String(v.id));
            if(!route || !layer) return;
            layer.setLatLngs(route.points.map(([px,py])=>this._planV3XyToLatLng(px,py,H)));
            const edge=(this.structEdges||[]).find(e2=>e2.id===v.edge_id);
            this._slPlaceLabel(v.id, v, edge, route.points, H);
          });
        },

      _slRefreshLineLabels(){
          if(!_slRouteCtx || !_planV3Map) return;
          const routes=SchemeLayout.routeEdges({items:_slRouteCtx.pos, edges:_slRouteCtx.edges}).routes;
          _slRouteCtx.views.forEach(v=>{
            const route=routes[v.id]; if(!route) return;
            const edge=(this.structEdges||[]).find(e2=>e2.id===v.edge_id);
            this._slPlaceLabel(v.id, v, edge, route.points, _slRouteCtx.H);
          });
        },

      // Вызывается из _planV3RenderBase при выборе плана: сброс чужих чисел, признак для печати,
      // первые P/загрузка/баланс сразу, а не через 5 секунд.
      _slOnRenderBase(W, H){
          const on=this.planV3IsScheme();
          document.documentElement.setAttribute('data-sl', on?'1':'0');
          _slBounds=on ? [[0,0],[H,W]] : null;
          if(!on){ this.slLoads={}; this.slBalance={}; return; }
          if(this._slPlanId!==this.planV3Current.id){
            this._slPlanId=this.planV3Current.id;
            this.slLoads={}; this.slBalance={}; this.slBalanceAt=0; this.slUndo=null;
          }
          this._slLoadBalances();
          this._slPollScheme();
        },

      _slPlanId:null,

      _slClear(){
          _slLabelLayers.forEach(l=>{ try{ if(_planV3Map) _planV3Map.removeLayer(l); }catch(e){} });
          _slLabelLayers.clear();
          _slRouteCtx=null;
        },

      // ---------------- живые числа: P, загрузка, баланс ----------------
      async _slPollScheme(){
          if(this.tab!=='planv3' || !this.planV3IsScheme()) return;
          const ids=[...new Set(((this.planV3Current||{}).edges||[]).map(v=>v.edge_id))];
          if(!ids.length) return;
          const t=this.feedStart('sl-live', {live:true, skipIfBusy:true});
          if(!t) return;
          try{
            const r=await fetch('/api/v2/topology/edge-loads?edge_ids='+ids.join(','), {signal:t.signal});
            if(!r.ok) throw new Error('HTTP '+r.status);
            const d=await r.json();
            if(!this.feedLive(t)) return;
            const m={}; (d.edges||[]).forEach(e=>{ m[e.edge_id]=e; });
            this.slLoads=m;
            this._slRefreshLineLabels();
            this.feedDone(t, 'ready');
            // баланс считается за период и не «живой»: раз в минуту
            if(Date.now()-this.slBalanceAt>60000) this._slLoadBalances();
          }catch(e){
            if(this.feedAborted(e) || !this.feedLive(t)) return;
            this.feedFail(t, e.message);
          }
        },

      // Значок показываем только когда баланс определён: «не определён» на каждом щите без
      // счётчика на вводе был бы шумом, причина видна в карточке щита («Баланс щита»).
      _slBalanceView(d){
          if(!d || d.unavailable_reason || d.value==null) return null;
          const pct=d.explanation && d.explanation.percentage!=null ? d.explanation.percentage : null;
          const sign=d.value>0?'+':'';
          const text='Δ '+sign+this.fmt(d.value,1)+' кВт·ч'
            +(pct!=null?(' ('+(pct>0?'+':'')+pct.toFixed(2)+'%)'):'');
          return {value:d.value, cls:d.value<0?'sl-bal-neg':'sl-bal-pos', text};
        },

      async _slLoadBalances(){
          if(!this.planV3IsScheme()) return;
          const panels=[...new Set(((this.planV3Current||{}).items||[])
            .filter(it=>it.kind==='node').map(it=>it.node_id))]
            .filter(id=>{ const n=(this.structNodes||[]).find(x=>x.id===id); return n && n.kind==='panel'; });
          if(!panels.length) return;
          if(!this.ovFrom || !this.ovTo){
            const d=this._ovDefaultDates(); this.ovFrom=this.ovFrom||d.from; this.ovTo=this.ovTo||d.to;
          }
          const t=this.feedStart('sl-bal', {live:true, skipIfBusy:true});
          if(!t) return;
          this.slBalanceAt=Date.now();
          const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
          try{
            const next={};
            for(const id of panels){
              const r=await fetch('/api/v2/topology/nodes/'+id+'/balance', {
                method:'POST', headers:{'Content-Type':'application/json'}, signal:t.signal,
                body: JSON.stringify({from:this.ovFrom, to:this.ovTo, timezone:tz})});
              if(!this.feedLive(t)) return;
              const view=r.ok ? this._slBalanceView(await r.json()) : null;
              if(view) next[id]=view;
            }
            this.slBalance=next;
            this._slRefreshTooltips();
            this.feedDone(t, 'ready');
          }catch(e){
            if(this.feedAborted(e) || !this.feedLive(t)) return;
            this.feedFail(t, e.message);
          }
        },

      _slRefreshTooltips(){
          ((this.planV3Current||{}).items||[]).forEach(it=>{
            if(it.kind!=='node') return;
            const layer=_planV3ItemLayers.get(String(it.id));
            const node=(this.structNodes||[]).find(n=>n.id===it.node_id);
            if(layer && node && layer.setTooltipContent) layer.setTooltipContent(this._slTipHtml(node, it));
          });
        },

      // ---------------- автораскладка ----------------
      _slPlacedFixed(){
          const fixed={};
          ((this.planV3Current||{}).items||[]).forEach(it=>{
            if(it.kind==='node' && it.geometry && typeof it.geometry.x==='number' && !(it.node_id in fixed)){
              fixed[it.node_id]={x:it.geometry.x, y:it.geometry.y};
            }
          });
          return fixed;
        },

      slAskAutoAll(){
          this.askConfirm({
            kind:'sl_auto_all', payload:{},
            title:'Перераскладить всю схему?',
            text:'Все значки встанут по ровной сетке: вводы сверху, щиты ниже, нагрузки внизу. '+
                 'Ручные трассы останутся и будут помечены, если перестанут идти по осям. '+
                 'Результат можно отменить кнопкой «Отменить автораскладку».',
            okLabel:'Перераскладить',
          });
        },

      async slAutoLayout(mode){
          if(this.slAutoBusy || !this.planV3IsScheme()) return false;
          this.slAutoBusy=true;
          try{
            if(!this.structInited){ this.structInited=true; await this._loadStructureLists(); }
            const plan=this.planV3Current;
            const [W,H]=this._planV3EffectiveDims(plan);
            const fixed=this._slPlacedFixed();
            const res=SchemeLayout.layoutTree(this._slLayoutInput({width:W, height:H}, mode, fixed));
            if(!res.fits){
              this.showToast('Схема не помещается в холст '+W+'×'+H+' (нужно не меньше '+res.needed.width+'×'+res.needed.height+
                '). Создайте новую схему кнопкой «+ Схема» — размер подберётся по топологии.', false);
              return false;
            }
            const todo=Object.keys(res.positions);
            if(mode==='unplaced' && !todo.length){
              this.showToast('Все элементы уже стоят на схеме', true); return true;
            }
            return await this._slSaveLayout(mode, res.positions);
          }catch(e){
            this.showToast('Ошибка раскладки: '+e, false); return false;
          }finally{ this.slAutoBusy=false; }
        },

      // Одна операция /layout: позиции + недостающие структурные виды линий.
      // Возвращает true — сохранено; false — нет (409 открывает окно конфликта).
      async _slSaveLayout(mode, positions){
          const plan=this.planV3Current;
          const items=plan.items||[];
          const itemOps=[], moved=[];
          const placedByNode={};
          items.forEach(it=>{
            if(it.kind==='node' && !(it.node_id in placedByNode)) placedByNode[it.node_id]=it;
          });
          const newIndex={};
          const created=[];
          Object.keys(positions).forEach(k=>{
            const nodeId=Number(k); const pos=positions[k];
            const geometry={x:pos.x, y:pos.y};
            const cur=placedByNode[nodeId];
            if(cur){
              moved.push({id:cur.id, geometry:cur.geometry, coord_space:cur.coord_space,
                          label:cur.label, sort_order:cur.sort_order||0});
              itemOps.push({op:'upsert', id:cur.id, geometry, coord_space:cur.coord_space,
                            label:cur.label, sort_order:cur.sort_order||0});
            }else{
              const node=(this.structNodes||[]).find(n=>n.id===nodeId);
              newIndex[nodeId]=itemOps.length;
              created.push(nodeId);
              itemOps.push({op:'upsert', kind:'node', node_id:nodeId, geometry,
                            coord_space:'canvas_xy_v2', label:node?node.name:''});
            }
          });
          const haveViews=new Set((plan.edges||[]).map(v=>v.edge_id));
          const ref=(nodeId)=> (nodeId in newIndex) ? ('$'+newIndex[nodeId])
            : (placedByNode[nodeId] ? placedByNode[nodeId].id : null);
          const viewOps=[];
          (this.structEdges||[]).forEach(e=>{
            if(haveViews.has(e.id)) return;
            const a=ref(e.from_node_id), b=ref(e.to_node_id);
            if(a==null || b==null) return;
            viewOps.push({op:'upsert', edge_id:e.id, from_item_id:a, to_item_id:b,
                          view_kind:'structural', waypoints:null});
          });
          const before={items:new Set(items.map(i=>i.id)), views:new Set((plan.edges||[]).map(v=>v.id))};
          const r=await fetch('/api/v2/plans/'+plan.id+'/layout', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({expected_revision:plan.canvas_revision,
                                  item_ops:itemOps, edge_view_ops:viewOps})});
          const d=await r.json().catch(()=>({}));
          if(r.status===409){
            this.slPendingMode=mode; this.slConflictOpen=true; return false;
          }
          if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return false; }
          await this.reloadPlanV3Items();
          const now=this.planV3Current||{};
          this.slUndo={
            planId:plan.id, moved,
            itemIds:(now.items||[]).filter(i=>!before.items.has(i.id)).map(i=>i.id),
            viewIds:(now.edges||[]).filter(v=>!before.views.has(v.id)).map(v=>v.id),
          };
          this.showToast('Раскладка применена: размещено '+created.length+', перемещено '+moved.length+
                         '. Её можно отменить.', true);
          this._slLoadBalances();
          return true;
        },

      // 409: чужие правки не затираем. Расчёт не потерян — «Разложить заново» перечитывает
      // схему и повторяет тот же режим на свежей ревизии.
      async slConflictRetry(){
          const mode=this.slPendingMode || 'unplaced';
          this.slConflictOpen=false;
          await this.reloadPlanV3Items();
          await this.slAutoLayout(mode);
        },

      slConflictClose(){ this.slConflictOpen=false; this.slPendingMode=null; },

      async slUndoAuto(){
          const u=this.slUndo;
          if(!u || !this.planV3Current || this.planV3Current.id!==u.planId || this.slAutoBusy) return;
          this.slAutoBusy=true;
          try{
            const itemOps=u.moved.map(m=>({op:'upsert', id:m.id, geometry:m.geometry,
              coord_space:m.coord_space, label:m.label, sort_order:m.sort_order}));
            u.itemIds.forEach(id=>itemOps.push({op:'remove', id}));
            const viewOps=u.viewIds.map(id=>({op:'remove', id}));
            const r=await fetch('/api/v2/plans/'+u.planId+'/layout', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({expected_revision:this.planV3Current.canvas_revision,
                                    item_ops:itemOps, edge_view_ops:viewOps})});
            const d=await r.json().catch(()=>({}));
            if(r.status===409){
              this.showToast('Схему изменили в другом окне — отмена не применена. Обновите схему.', false);
              await this.reloadPlanV3Items(); return;
            }
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            this.slUndo=null;
            await this.reloadPlanV3Items();
            this.showToast('Автораскладка отменена: прежние положения возвращены', true);
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
          finally{ this.slAutoBusy=false; }
        },

      // ---------------- ручная трасса ----------------
      slViewOfEdge(edgeId){
          return ((this.planV3Current||{}).edges||[]).find(v=>v.edge_id===edgeId) || null;
        },

      slEdgeIsManual(edgeId){
          const v=this.slViewOfEdge(edgeId);
          return !!(v && Array.isArray(v.waypoints) && v.waypoints.length>=2);
        },

      slEdgeIsBad(edgeId){
          const v=this.slViewOfEdge(edgeId);
          if(!v || !_slRouteCtx) return false;
          const e=_slRouteCtx.edges.find(x=>x.id===v.id);
          if(!e || !e.waypoints) return false;
          const r=SchemeLayout.routeEdges({items:_slRouteCtx.pos, edges:[e]}).routes[v.id];
          return !!(r && !r.orthogonal);
        },

      slStartDrawTrace(edgeId){
          const v=this.slViewOfEdge(edgeId);
          if(!v || !_planV3Map || !this.planV3IsScheme()){
            this.showToast('Линия не нарисована на этой схеме', false); return;
          }
          this.planV3CancelTool();
          this.planV3Tool='sl-trace';
          this.slDrawingEdgeId=edgeId;
          if(_planV3Map.doubleClickZoom) _planV3Map.doubleClickZoom.disable();
          _planV3Map.pm.enableDraw('Line', {snappable:false, finishOn:'dblclick'});
          this.showToast('Кликайте по углам трассы рядом со значками, двойной клик — завершить (Esc — отмена). Отрезки встанут по осям.', true);
        },

      _slCancelDraw(){
          if(this.slDrawingEdgeId==null) return;
          if(_planV3Map && _planV3Map.pm) _planV3Map.pm.disableDraw('Line');
          if(_planV3Map && _planV3Map.doubleClickZoom) _planV3Map.doubleClickZoom.enable();
          this.slDrawingEdgeId=null;
          if(this.planV3Tool==='sl-trace') this.planV3Tool=null;
        },

      async _slOnLineDrawn(e){
          const edgeId=this.slDrawingEdgeId;
          const v=this.slViewOfEdge(edgeId);
          const pts=e.layer.getLatLngs();
          try{ _planV3Map.removeLayer(e.layer); }catch(err){}
          this._slCancelDraw();
          if(!v || !_slRouteCtx || !pts || pts.length<2){
            this.showToast('Трасса не сохранена: нужно минимум две точки', false); return;
          }
          const H=_slRouteCtx.H;
          const a=_slRouteCtx.pos[v.from_item_id], b=_slRouteCtx.pos[v.to_item_id];
          if(!a || !b){ this.showToast('Концы линии не стоят на схеме', false); return; }
          // Все клики — углы трассы: начало и конец достраиваются от значков сами.
          const drawn=pts.map(ll=>this._planV3LatLngToXy(ll, H));
          const path=SchemeLayout.snapOrthogonal(drawn, a, b).map(([x,y])=>[Math.round(x), Math.round(y)]);
          await this._slSaveWaypoints(v, path, 'Трасса сохранена');
        },

      async slMakeAuto(edgeId){
          const v=this.slViewOfEdge(edgeId);
          if(!v) return;
          await this._slSaveWaypoints(v, null, 'Трасса снова автоматическая');
        },

      // Одна операция /layout по ревизии плана: пишем waypoints вида линии.
      async _slSaveWaypoints(v, waypoints, okText){
          if(this.slTraceBusy) return;
          this.slTraceBusy=true;
          try{
            const plan=this.planV3Current;
            const r=await fetch('/api/v2/plans/'+plan.id+'/layout', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({expected_revision:plan.canvas_revision, item_ops:[],
                edge_view_ops:[{op:'upsert', id:v.id, edge_id:v.edge_id, from_item_id:v.from_item_id,
                                to_item_id:v.to_item_id, view_kind:v.view_kind||'structural', waypoints}]})});
            const d=await r.json().catch(()=>({}));
            if(r.status===409){
              this.showToast('Схему изменили в другом окне — трасса не сохранена. Схема обновлена, повторите.', false);
              await this.reloadPlanV3Items(); return;
            }
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            this.showToast(okText, true);
            await this.reloadPlanV3Items();
          }catch(err){ this.showToast('Ошибка сети: '+err, false); }
          finally{ this.slTraceBusy=false; }
        },

      slTraceBusy:false,

      // ---------------- печать ----------------
      slPrint(){ window.print(); },
    };
  });
})();

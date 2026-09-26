// plan-v1.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
// ---- модульные переменные/хелперы только этого экрана ----
let _planMap=null;
let _planImageLayer=null;
const _planZoneLayers=new Map();
const _planLinkLayers=new Map();
let _planDrawLayer=null;
let _planOpenPopupGroupId=null;
function _planLinkColor(state){
  return ({ok:_planCssVar('--ok'), warn:_planCssVar('--warn'),
           danger:_planCssVar('--err')})[state] || _planCssVar('--accent2');
}

(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function planV1Part() {
    return {
      planPlans:[],

      planCurrentId:null,

      planCurrent:null,

      planLive:{zones:[],links:[]},

      planLoading:false,

      planEditMode:false,

      planPeriod:'today',

      planUploadName:'',

      planUploadBusy:false,

      planUploadError:'',

      planDrawingGroupId:null,

      planHasPendingZoneDraw:false,

      planAddingLink:false,

      planLinkFromZoneId:null,

      planLinkForm:{toZoneId:'', label:'', sourceMeterId:'', ratedCurrentA:''},

      planConfirmRemoveZone:null,

      planConfirmDeleteLink:null,

      planConfirmDeletePlan:null,

      planInited:false,

      planPickerChoices:null,

      initPlanMap(){
          if(_planMap) return; // контейнер не пересоздаётся (x-show, не x-if) —
                                // карта создаётся ровно один раз за жизнь страницы
          const el=this.$refs.planMap;
          _planMap=L.map(el, {crs:L.CRS.Simple, minZoom:-4, zoomControl:true,
                              attributionControl:false});
          _planMap.setView([0,0],0);
          _planMap.on('pm:create', (e)=>this._planOnPmCreate(e));
          _planMap.on('popupopen', (e)=>{
            const wrap=e.popup.getElement();
            const link=wrap && wrap.querySelector('.pp-link');
            if(link){
              link.onclick=(ev)=>{
                ev.preventDefault();
                if(_planOpenPopupGroupId!=null) this.openZoneOnDashboard(_planOpenPopupGroupId);
              };
            }
          });
          window.addEventListener('resize', ()=>{ if(_planMap) _planMap.invalidateSize(); });
        },

      async openPlanTab(){
          if(!_planMap) this.initPlanMap();
          if(!this.planInited){
            this.planInited=true;
            await this.reloadGroups();
            await this.loadPlanList();
          }
          this.$nextTick(()=>{ if(_planMap) _planMap.invalidateSize(); });
        },

      async loadPlanList(){
          this.planLoading=true;
          try{
            const r=await fetch('/api/plans'); const d=await r.json();
            this.planPlans=d.items||[];
            if(this.planPlans.length>0 && !this.planCurrentId){
              const def=this.planPlans.find(p=>p.is_default)||this.planPlans[0];
              await this.selectPlan(def.id);
            }
          }catch(e){ console.error(e); }
          this.planLoading=false;
        },

      async selectPlan(id){
          if(!id) return;
          this.cancelZoneDraw(); this.cancelAddLink();
          this.planCurrentId=id;
          try{
            const r=await fetch('/api/plans/'+id);
            if(!r.ok){ this.showToast('Не удалось загрузить план',false); return; }
            this.planCurrent=await r.json();
          }catch(e){ console.error(e); return; }
          this._planRenderBase();
          await this.loadPlanLive();
        },

      async loadPlanLive(){
          if(!this.planCurrentId) return;
          try{
            const r=await fetch(`/api/plans/${this.planCurrentId}/live?period=${encodeURIComponent(this.planPeriod)}`);
            if(!r.ok) return;
            this.planLive=await r.json();
          }catch(e){ console.error(e); return; }
          this._planApplyLiveStyles();
        },

      setPlanPeriod(id){ this.planPeriod=id; this.loadPlanLive(); },

      togglePlanEdit(){
          this.planEditMode=!this.planEditMode;
          if(!this.planEditMode){ this.cancelZoneDraw(); this.cancelAddLink(); }
          this.$nextTick(()=>{ if(_planMap) _planMap.invalidateSize(); });
        },

      isZoneOnPlan(groupId){
          return !!(this.planCurrent && (this.planCurrent.zones||[]).some(z=>z.group_id===groupId));
        },

      zoneNameById(zoneId){
          const z=this.planCurrent && (this.planCurrent.zones||[]).find(zz=>zz.id===zoneId);
          return z? z.group_name : null;
        },

      _planLiveForGroup(groupId){
          return (this.planLive.zones||[]).find(z=>z.group_id===groupId) || null;
        },

      openZoneOnDashboard(groupId){
          const z=(this.zones||[]).find(g=>g.id===groupId);
          const name=z?z.name:null;
          this.uiSet='classic'; // 'dash' — вкладка классического набора, см. UI_SET_TABS
          this.tab='dash';
          this.$nextTick(()=>{
            if(!name) return;
            const target=[...document.querySelectorAll('.zone-title')]
              .find(elx=>elx.textContent===name);
            if(target) target.scrollIntoView({behavior:'smooth', block:'start'});
          });
        },

      _planClearLayers(){
          if(!_planMap) return;
          if(_planImageLayer){ try{_planMap.removeLayer(_planImageLayer);}catch(e){} _planImageLayer=null; }
          _planZoneLayers.forEach(rec=>{ try{_planMap.removeLayer(rec.layer);}catch(e){} });
          _planZoneLayers.clear();
          _planLinkLayers.forEach(line=>{ try{_planMap.removeLayer(line);}catch(e){} });
          _planLinkLayers.clear();
          _planOpenPopupGroupId=null;
        },

      _planZoneAnchorLatLng(zone){
          if(zone.anchor) return L.latLng(zone.anchor[0], zone.anchor[1]);
          const rec=_planZoneLayers.get(zone.group_id);
          if(rec){
            if(rec.layer.getBounds) return rec.layer.getBounds().getCenter();
            if(rec.layer.getLatLng) return rec.layer.getLatLng();
          }
          return L.latLng(0,0);
        },

      _planRenderBase(){
          if(!_planMap || !this.planCurrent) return;
          this._planClearLayers();
          const pc=this.planCurrent;
          const bounds=[[0,0],[pc.image_height, pc.image_width]];
          _planImageLayer=L.imageOverlay('/api/plans/'+pc.id+'/image', bounds).addTo(_planMap);
          _planMap.fitBounds(bounds);
      
          (pc.zones||[]).forEach(z=>{
            const color=z.color||'#3dd6c0';
            let layer;
            if(z.shape_type==='marker'){
              layer=L.circleMarker(z.geometry, {radius:10, color, fillColor:color,
                                                fillOpacity:.5, weight:2});
            }else{
              layer=L.polygon(z.geometry, {color, weight:2, fillColor:color, fillOpacity:.25});
            }
            const popup=L.popup({className:'plan-popup-wrap', maxWidth:270, autoPan:true});
            layer.on('click', (e)=>{
              if(this.planAddingLink){ this._planHandleLinkZoneClick(z.group_id); return; }
              _planOpenPopupGroupId=z.group_id;
              popup.setLatLng(e.latlng)
                   .setContent(this._planPopupHtml(z, this._planLiveForGroup(z.group_id)))
                   .openOn(_planMap);
            });
            layer.addTo(_planMap);
            _planZoneLayers.set(z.group_id, {layer, popup, zone:z});
          });
      
          (pc.links||[]).forEach(l=>{
            const fz=(pc.zones||[]).find(z=>z.id===l.from_zone_id);
            const tz=(pc.zones||[]).find(z=>z.id===l.to_zone_id);
            if(!fz || !tz) return;
            const fromLL=this._planZoneAnchorLatLng(fz);
            const toLL=this._planZoneAnchorLatLng(tz);
            const mid=(l.waypoints||[]).map(p=>L.latLng(p[0],p[1]));
            const line=L.polyline([fromLL, ...mid, toLL],
              {color:_planCssVar('--accent2'), weight:3, opacity:.85});
            line.addTo(_planMap);
            _planLinkLayers.set(l.id, line);
          });
      
          this._planApplyLiveStyles();
        },

      _planPopupHtml(zone, live){
          const name=_planEscapeHtml(zone.group_name||'');
          const color=zone.color||'#3dd6c0';
          const power=live && live.power_kw!=null ? live.power_kw.toFixed(3) : '—';
          const cons=live && live.consumption_kwh!=null ? live.consumption_kwh.toFixed(2) : '—';
          const total=live ? live.meters_total : 0;
          const ok=live ? live.meters_ok : 0;
          let worstHtml='';
          if(live && live.worst_status && live.worst_status!=='ok'){
            worstHtml=`<div class="pp-worst">${_planEscapeHtml(this.statusLabel(live.worst_status))}</div>`;
          }
          return `<div class="plan-popup">`+
            `<div class="pp-head"><span class="pp-dot" style="background:${color}"></span>`+
            `<span class="pp-title">${name}</span></div>`+
            `<div class="pp-power">${power}<small> кВт</small></div>`+
            `<div class="pp-row"><span>Счётчиков</span><span>${ok}/${total} в порядке</span></div>`+
            `<div class="pp-row"><span>Расход за период</span><span>${cons} кВт·ч</span></div>`+
            worstHtml+
            `<a class="pp-link" href="#">Открыть счётчики зоны →</a></div>`;
        },

      _planApplyLiveStyles(){
          if(!_planMap) return;
          const zoneLive={};
          (this.planLive.zones||[]).forEach(z=>{ zoneLive[z.group_id]=z; });
          _planZoneLayers.forEach((rec, groupId)=>{
            const zl=zoneLive[groupId]||null;
            const alarm=!!(zl && zl.worst_status && zl.worst_status!=='ok');
            const path=rec.layer._path;
            if(path) path.classList.toggle('plan-zone-alarm', alarm);
            // Только контент попапа, слой НЕ пересоздаётся — открытый попап не закроется.
            rec.popup.setContent(this._planPopupHtml(rec.zone, zl));
          });
          const linkLive={};
          (this.planLive.links||[]).forEach(l=>{ linkLive[l.id]=l; });
          _planLinkLayers.forEach((line, linkId)=>{
            const ll=linkLive[linkId]||null;
            const weight=ll && ll.weight!=null ? (2 + ll.weight*8) : 3;
            const color=ll ? _planLinkColor(ll.state) : _planCssVar('--accent2');
            line.setStyle({weight, color, opacity:.85});
            const linkMeta=(this.planCurrent && (this.planCurrent.links||[]).find(x=>x.id===linkId));
            const parts=[];
            if(linkMeta && linkMeta.label) parts.push(_planEscapeHtml(linkMeta.label));
            if(ll && ll.current_a!=null) parts.push(ll.current_a.toFixed(1)+' А');
            if(ll && ll.load_pct!=null) parts.push(ll.load_pct+'%');
            const tip=parts.join(' · ')||'Связь';
            if(line.getTooltip()) line.setTooltipContent(tip);
            else line.bindTooltip(tip, {sticky:true});
          });
        },

      startZoneDraw(group){
          if(this.planDrawingGroupId!==null || !_planMap) return;
          this.planDrawingGroupId=group.id;
          this.planHasPendingZoneDraw=false;
          // Баг из браузера (§5): встроенный Leaflet doubleClickZoom конкурирует
          // с geoman finishOn:'dblclick' за один и тот же двойной клик — второй
          // клик то зумит карту, то не долетает до geoman, и фигура "не
          // завершается" на глаз пользователя. На время рисования отключаем
          // zoom по двойному клику, возвращаем в cancelZoneDraw()/saveZoneDraw()
          // (обе — единственные выходы из режима, включая Escape, см. init()).
          if(_planMap.doubleClickZoom) _planMap.doubleClickZoom.disable();
          _planMap.pm.enableDraw('Polygon', {snappable:true, finishOn:'dblclick'});
        },

      _planOnPmCreate(e){
          if(e.shape==='Polygon' && this.planDrawingGroupId!==null){
            if(_planDrawLayer){ try{_planMap.removeLayer(_planDrawLayer);}catch(err){} }
            _planDrawLayer=e.layer;
            if(_planDrawLayer.pm) _planDrawLayer.pm.enable({allowSelfIntersection:false});
            this.planHasPendingZoneDraw=true;
          }
        },

      async saveZoneDraw(){
          if(!_planDrawLayer || this.planDrawingGroupId===null) return;
          const rings=_planDrawLayer.getLatLngs();
          const ring=Array.isArray(rings[0]) ? rings[0] : rings;
          const geometry=ring.map(ll=>[ll.lat, ll.lng]);
          const groupId=this.planDrawingGroupId;
          try{
            const r=await fetch(`/api/plans/${this.planCurrentId}/zones/${groupId}`,
              {method:'PUT', headers:{'Content-Type':'application/json'},
               body:JSON.stringify({shape_type:'polygon', geometry})});
            const d=await r.json().catch(()=>({}));
            if(!r.ok){ this.showToast(d.error||'Не удалось сохранить контур',false); return; }
            this.showToast('Контур зоны сохранён');
            this.cancelZoneDraw();
            await this.selectPlan(this.planCurrentId);
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        },

      cancelZoneDraw(){
          if(_planMap && _planMap.pm) _planMap.pm.disableDraw('Polygon');
          if(_planMap && _planMap.doubleClickZoom) _planMap.doubleClickZoom.enable();
          if(_planDrawLayer){ try{_planMap.removeLayer(_planDrawLayer);}catch(e){} _planDrawLayer=null; }
          this.planDrawingGroupId=null;
          this.planHasPendingZoneDraw=false;
        },

      async confirmRemoveZoneFromPlan(){
          const target=this.planConfirmRemoveZone; this.planConfirmRemoveZone=null;
          if(!target) return;
          try{
            const r=await fetch(`/api/plans/${this.planCurrentId}/zones/${target.group_id}`,
              {method:'DELETE'});
            if(!r.ok){ const d=await r.json().catch(()=>({})); this.showToast(d.error||'Ошибка',false); return; }
            this.showToast('Зона убрана с плана');
            await this.selectPlan(this.planCurrentId);
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        },

      toggleAddLink(){
          if(this.planAddingLink){ this.cancelAddLink(); return; }
          this.planAddingLink=true;
          this.planLinkFromZoneId=null;
          this.planLinkForm={toZoneId:'', label:'', sourceMeterId:'', ratedCurrentA:''};
        },

      cancelAddLink(){
          this.planAddingLink=false;
          this.planLinkFromZoneId=null;
          this.planLinkForm={toZoneId:'', label:'', sourceMeterId:'', ratedCurrentA:''};
        },

      _planHandleLinkZoneClick(groupId){
          const zone=this.planCurrent && (this.planCurrent.zones||[]).find(z=>z.group_id===groupId);
          if(!zone){ this.showToast('У этой зоны ещё нет контура на плане', false); return; }
          if(!this.planLinkFromZoneId){
            this.planLinkFromZoneId=zone.id;
            this.showToast('Зона-источник выбрана, кликните по зоне-приёмнику', true);
            return;
          }
          if(zone.id===this.planLinkFromZoneId){
            this.showToast('Зона-приёмник должна отличаться от источника', false);
            return;
          }
          this.planLinkForm.toZoneId=zone.id;
        },

      async saveLink(){
          if(!this.planLinkFromZoneId || !this.planLinkForm.toZoneId) return;
          const body={
            from_zone_id:this.planLinkFromZoneId,
            to_zone_id:this.planLinkForm.toZoneId,
            label:this.planLinkForm.label || null,
            source_meter_id:this.planLinkForm.sourceMeterId ? Number(this.planLinkForm.sourceMeterId) : null,
            rated_current_a:this.planLinkForm.ratedCurrentA!==''
              ? Number(this.planLinkForm.ratedCurrentA) : null,
          };
          try{
            const r=await fetch(`/api/plans/${this.planCurrentId}/links`,
              {method:'POST', headers:{'Content-Type':'application/json'},
               body:JSON.stringify(body)});
            const d=await r.json().catch(()=>({}));
            if(!r.ok){ this.showToast(d.error||'Не удалось сохранить связь',false); return; }
            this.showToast('Связь создана');
            this.cancelAddLink();
            await this.selectPlan(this.planCurrentId);
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        },

      async confirmDeletePlanLink(){
          const target=this.planConfirmDeleteLink; this.planConfirmDeleteLink=null;
          if(!target) return;
          try{
            const r=await fetch(`/api/plans/${this.planCurrentId}/links/${target.id}`,
              {method:'DELETE'});
            if(!r.ok){ const d=await r.json().catch(()=>({})); this.showToast(d.error||'Ошибка',false); return; }
            this.showToast('Связь удалена');
            await this.selectPlan(this.planCurrentId);
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        },

      async uploadPlan(){
          const input=this.$refs.planFileInput;
          const file=input && input.files && input.files[0];
          this.planUploadError='';
          if(!file){ this.planUploadError='Выберите файл'; return; }
          if(!this.planUploadName.trim()){ this.planUploadError='Укажите название плана'; return; }
          this.planUploadBusy=true;
          try{
            const fd=new FormData();
            fd.append('name', this.planUploadName.trim());
            fd.append('file', file);
            const r=await fetch('/api/plans', {method:'POST', body:fd});
            const d=await r.json().catch(()=>({}));
            if(!r.ok){
              this.planUploadError=d.error||'Не удалось загрузить план';
              this.planUploadBusy=false; return;
            }
            this.showToast('План загружен');
            this.planUploadName=''; if(input) input.value='';
            this.planCurrentId=null; // чтобы loadPlanList не оставил старый выбор
            await this.loadPlanList();
            await this.selectPlan(d.id);
          }catch(e){ this.planUploadError='Ошибка: '+e; }
          this.planUploadBusy=false;
        },

      async renamePlanPrompt(){
          if(!this.planCurrent) return;
          const name=prompt('Новое имя плана', this.planCurrent.name);
          if(!name || !name.trim()) return;
          try{
            const r=await fetch(`/api/plans/${this.planCurrent.id}`,
              {method:'PATCH', headers:{'Content-Type':'application/json'},
               body:JSON.stringify({name:name.trim()})});
            const d=await r.json().catch(()=>({}));
            if(!r.ok){ this.showToast(d.error||'Ошибка',false); return; }
            this.showToast('План переименован');
            await this.loadPlanList();
            await this.selectPlan(this.planCurrentId);
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        },

      async setDefaultPlan(){
          if(!this.planCurrent) return;
          try{
            const r=await fetch(`/api/plans/${this.planCurrent.id}`,
              {method:'PATCH', headers:{'Content-Type':'application/json'},
               body:JSON.stringify({is_default:true})});
            if(!r.ok){ const d=await r.json().catch(()=>({})); this.showToast(d.error||'Ошибка',false); return; }
            this.showToast('План назначен по умолчанию');
            await this.loadPlanList();
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        },

      async confirmDeletePlanNow(){
          const target=this.planConfirmDeletePlan; this.planConfirmDeletePlan=null;
          if(!target) return;
          try{
            const r=await fetch(`/api/plans/${target.id}`, {method:'DELETE'});
            if(!r.ok){ const d=await r.json().catch(()=>({})); this.showToast(d.error||'Ошибка',false); return; }
            this.showToast('План удалён');
            this._planClearLayers();
            this.planCurrentId=null; this.planCurrent=null;
            this.planPlans=this.planPlans.filter(p=>p.id!==target.id);
            await this.loadPlanList();
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        },

      async _planEdgeLiveMetrics(edge){
          if(!edge || edge.primary_point_id==null) return {currentA:null, loadPct:null, phases:null};
          try{
            const sr=await fetch('/api/v2/snapshot?point_ids='+edge.primary_point_id);
            if(!sr.ok) return {currentA:null, loadPct:null, phases:null};
            const snap=(await sr.json()).points||[];
            const point=snap[0]||null;
            const cur=point && point.current_a;
            if(!cur) return {currentA:null, loadPct:null, phases:null, power_w: point?point.power_w:null};
            const vals=[cur.L1,cur.L2,cur.L3].filter(x=>x!=null);
            if(!vals.length) return {currentA:null, loadPct:null, phases:cur, power_w: point.power_w};
            const currentA=Math.max(...vals);
            const loadPct=edge.rated_current_a ? Math.round(currentA/edge.rated_current_a*100) : null;
            return {currentA, loadPct, phases:cur, power_w: point.power_w};
          }catch(e){ return {currentA:null, loadPct:null, phases:null}; }
        }
    };
  });
})();

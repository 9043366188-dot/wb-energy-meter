// dashboard.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
// ---- модульные переменные/хелперы только этого экрана ----
function lsGetJSON(k,def){
  try{ const v=localStorage.getItem(k); return v?JSON.parse(v):def; }catch(e){ return def; }
}
const FALLBACK_CATEGORIES=[
  {id:'voltage',label:'Напряжение',open:false},
  {id:'current',label:'Ток',open:false},
  {id:'power',label:'Мощность',open:false},
  {id:'energy',label:'Энергия',open:false},
  {id:'quality',label:'Качество сети',open:false},
  {id:'service',label:'Служебные',open:false},
  {id:'other',label:'Прочее',open:false},
];
function dtLocal(d){
  const p=n=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtSeconds(s){
  s=Math.round(s); if(s<0) return '—';
  if(s<60) return s+'с';
  if(s<3600){ return Math.floor(s/60)+'мин'; }
  if(s<86400){ const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
    return h+'ч'+(m?' '+m+'мин':''); }
  const d=Math.floor(s/86400),h=Math.floor((s%86400)/3600);
  return d+'д'+(h?' '+h+'ч':'');
}

(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function dashboardPart() {
    return {
      meters:[],

      byStatus:{},

      mqtt:{connected:false,messages:0,errors:0},

      detail:null,

      chartBars:[],

      chartSvg:'',

      chartMeterName:'',

      channelCategories:[],

      detailChannel:null,

      detailChartLoading:false,

      detailChartError:'',

      detailChartData:null,

      detailChartSvg:'',

      detailChartPeriod:'today',

      detailChartFrom:'',

      detailChartTo:'',

      detailCatsOpen:lsGetJSON('wbem.detailCats',{}),

      _detailChartSeq:0,

      _detailChartLastCall:0,

      get groupedMeters(){
          const groups = {};
          this.meters.forEach(m=>{
            const z = m.group || '— Без зоны —';
            if(!groups[z]) groups[z]={name:z,meters:[]};
            groups[z].meters.push(m);
          });
          // Сортировка: сначала именованные зоны, потом "Без зоны"
          const named = Object.keys(groups).filter(k=>k!=='— Без зоны —').sort();
          const order = [...named, '— Без зоны —'].filter(k=>groups[k]);
          return order.map(k=>groups[k]);
        },

      zonePower(meters){
          let s=0;
          meters.forEach(m=>{const v=m.main_values&&m.main_values['Total P'];if(typeof v=='number')s+=v;});
          return s.toFixed(3);
        },

      get totalPowerKw(){
          let s=0; this.meters.forEach(m=>{
            const v=m.main_values&&m.main_values['Total P']; if(typeof v=='number') s+=v;
          }); return s.toFixed(3);
        },

      get totalEnergy(){
          let s=0; this.meters.forEach(m=>{
            const v=m.main_values&&m.main_values['Total AP energy']; if(typeof v=='number') s+=v;
          }); return s.toFixed(2);
        },

      get uptimeStr(){
          const s=this.uptime;
          if(s<3600) return Math.floor(s/60)+'м';
          if(s<86400) return (s/3600).toFixed(1)+'ч';
          return (s/86400).toFixed(1)+'д';
        },

      meterUptime(m){
          const v=(m.main_values&&m.main_values['Uptime (s)'])??
                  (m.main_values&&m.main_values['Uptime']);
          if(v==null||isNaN(v)) return null;
          return fmtSeconds(v);
        },

      async openDetail(devId){
          this.detailChannel=null; this.detailChartData=null; this.detailChartError='';
          try{ const r=await fetch('/api/meters/'+encodeURIComponent(devId));
            this.detail=await r.json(); }catch(e){ console.error(e); }
          if(this.tab=='consumption') this.loadHourly(devId);
        },

      closeDetail(){
          this.detail=null; this.detailChannel=null;
          this.detailChartData=null; this.detailChartError=''; this.detailChartSvg='';
        },

      get detailMainControls(){
          if(!this.detail || !this.detail.controls) return [];
          return Object.entries(this.detail.controls)
            .filter(([name,c])=>c.main)
            .map(([name,c])=>({name,...c}));
        },

      get detailOtherCategories(){
          if(!this.detail || !this.detail.controls) return [];
          const byCat={};
          Object.entries(this.detail.controls).forEach(([name,c])=>{
            if(c.main) return;
            const cat=c.category||'other';
            if(!byCat[cat]) byCat[cat]=[];
            byCat[cat].push({name,...c});
          });
          const meta=(this.channelCategories&&this.channelCategories.length)?
            this.channelCategories:FALLBACK_CATEGORIES;
          return meta.filter(cc=>cc.id!=='main')
            .map(cc=>({id:cc.id,label:cc.label,open:cc.open,items:byCat[cc.id]||[]}))
            .filter(cc=>cc.items.length>0);
        },

      _catDefaultOpen(id){
          const meta=(this.channelCategories&&this.channelCategories.length)?
            this.channelCategories:FALLBACK_CATEGORIES;
          const c=meta.find(cc=>cc.id===id);
          return c?!!c.open:false;
        },

      isDetailCatOpen(id){
          const cur=this.detailCatsOpen[id];
          return cur===undefined?this._catDefaultOpen(id):cur;
        },

      toggleDetailCat(id){
          const isOpen=this.isDetailCatOpen(id);
          this.detailCatsOpen={...this.detailCatsOpen,[id]:!isOpen};
          try{ localStorage.setItem('wbem.detailCats',JSON.stringify(this.detailCatsOpen)); }catch(e){}
        },

      openChannelChart(name){
          this.detailChannel=name;
          this.detailChartPeriod='today'; this.detailChartFrom=''; this.detailChartTo='';
          this.detailChartData=null; this.detailChartError=''; this.detailChartSvg='';
          this.loadDetailChart();
        },

      closeChannelChart(){
          this.detailChannel=null;
          this.detailChartData=null; this.detailChartError=''; this.detailChartSvg='';
        },

      detailChartQuery(){
          if(this.detailChartPeriod==='hour'){
            const to=new Date(), from=new Date(to.getTime()-3600*1000);
            return 'from='+dtLocal(from)+'&to='+dtLocal(to);
          }
          return this._rPeriodQuery(this.detailChartPeriod, this.detailChartFrom, this.detailChartTo);
        },

      async loadDetailChart(){
          if(!this.detail || !this.detailChannel) return;
          const myReq=++this._detailChartSeq;
          // Троттлинг ~300мс между запросами (известное ограничение — см.
          // CHANGELOG: wb_db_client.WbDbClient открывает новое MQTT-соединение
          // на каждый RPC-вызов, частые клики по параметрам иначе дают шквал
          // подключений к брокеру).
          const wait=Math.max(0,300-(Date.now()-this._detailChartLastCall));
          if(wait>0) await new Promise(res=>setTimeout(res,wait));
          if(myReq!==this._detailChartSeq) return;
          this._detailChartLastCall=Date.now();
          this.detailChartLoading=true; this.detailChartError='';
          this.detailChartData=null; this.detailChartSvg='';
          const q=this.detailChartQuery();
          try{
            const r=await fetch('/api/meters/'+encodeURIComponent(this.detail.device_id)+
              '/channel-history?control='+encodeURIComponent(this.detailChannel)+'&'+q);
            if(myReq!==this._detailChartSeq) return;
            if(!r.ok){
              let e={}; try{ e=await r.json(); }catch(_){}
              this.detailChartError=e.detail||e.error||'История недоступна: wb-mqtt-db не отвечает';
              this.detailChartLoading=false; return;
            }
            const d=await r.json();
            if(myReq!==this._detailChartSeq) return;
            this.detailChartData=d;
            this.renderDetailChartSvg();
          }catch(e){
            if(myReq===this._detailChartSeq)
              this.detailChartError='История недоступна: wb-mqtt-db не отвечает';
          }
          if(myReq===this._detailChartSeq) this.detailChartLoading=false;
        },

      renderDetailChartSvg(){
          const d=this.detailChartData;
          if(!d||!d.items||!d.items.length){ this.detailChartSvg=''; return; }
          const pts=d.items;
          const W=Math.max(700,Math.min(pts.length*4,1400)), H=220,
            padL=52, padR=14, padT=16, padB=28;
          const ts=pts.map(p=>p.t), vs=pts.map(p=>p.v);
          const tMin=Math.min(...ts), tMax=Math.max(...ts);
          const vMin=Math.min(...vs), vMax=Math.max(...vs);
          const vSpan=(vMax-vMin)||1, tSpan=(tMax-tMin)||1;
          const X=t=>padL+((t-tMin)/tSpan)*(W-padL-padR);
          const Y=v=>H-padB-((v-vMin)/vSpan)*(H-padT-padB);
          let svg=`<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px">`;
          for(let i=0;i<=4;i++){
            const y=padT+(H-padT-padB)*i/4;
            const val=vMax-(vSpan*i/4);
            svg+=`<line x1="${padL}" y1="${y}" x2="${W-padR}" y2="${y}" stroke="var(--line)" stroke-dasharray="3,3"/>`;
            svg+=`<text x="${padL-4}" y="${y+4}" fill="var(--txt3)" font-size="10" text-anchor="end">${val.toFixed(2)}</text>`;
          }
          svg+=`<line x1="${padL}" y1="${H-padB}" x2="${W-padR}" y2="${H-padB}" stroke="var(--line)"/>`;
          const path=pts.map((p,i)=>(i===0?'M':'L')+X(p.t).toFixed(1)+','+Y(p.v).toFixed(1)).join(' ');
          svg+=`<path d="${path}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>`;
          [0, Math.floor(pts.length/2), pts.length-1].forEach(i=>{
            const p=pts[i]; if(!p) return;
            svg+=`<text x="${X(p.t)}" y="${H-padB+14}" fill="var(--txt3)" font-size="9" text-anchor="middle">${fmtHour(p.t)}</text>`;
          });
          svg+=`</svg>`;
          this.detailChartSvg=svg;
        },

      downloadDetailChart(){
          const d=this.detailChartData;
          if(!d||!d.items||!d.items.length) return;
          const lines=['﻿',
            `wb-energy-meter — История ${d.label||d.control} (${d.device_id})`,'',
            'Время;Значение'+(d.units?(' ('+d.units+')'):'')];
          d.items.forEach(p=>{ lines.push([this.csvCell(fmtHour(p.t)),p.v].join(';')); });
          const blob=new Blob([lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
          const a=document.createElement('a');
          a.href=URL.createObjectURL(blob);
          a.download=(`channel_${d.device_id}_${d.control}.csv`).replace(/[^\w.\-]+/g,'_');
          a.click();
        }
    };
  });
})();

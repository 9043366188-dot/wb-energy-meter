// consumption.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function consumptionPart() {
    return {
      consLoading:false,

      consItems:[],

      consTotal:0,

      consAnyUnknown:false,

      period:{preset:'this_month',from:'',to:''},

      presets:[
          {id:'today',label:'Сегодня'},{id:'yesterday',label:'Вчера'},
          {id:'this_month',label:'Этот месяц'},{id:'last_month',label:'Прошлый месяц'},
          {id:'last_7d',label:'7 дней'},{id:'last_30d',label:'30 дней'},
        ],

      rMode:'vedomost',

      rLoading:false,

      rPeriod:'this_month',

      rFrom:'',

      rTo:'',

      vedomostRows:[],

      rTotal:0,

      rPeriodLabel:'',

      profileMeterId:'',

      profilePeriod:'last_7d',

      profileFrom:'',

      profileTo:'',

      profileHours:[],

      profileTotal:0,

      profilePeak:{v:0,label:'',item:null},

      profileSvg:'',

      profileLoading:false,

      profilePresets:[
          {id:'today',label:'Сегодня'},{id:'yesterday',label:'Вчера'},
          {id:'last_7d',label:'7 дней'},{id:'this_month',label:'Этот месяц'},
          {id:'last_30d',label:'30 дней'},
        ],

      cmpPeriodA:'this_month',

      cmpPeriodB:'last_month',

      cmpRows:[],

      cmpTotalA:0,

      cmpTotalB:0,

      cmpLabelA:'',

      cmpLabelB:'',

      cmpLoading:false,

      setPreset(id){ this.period.preset=id; this.period.from=''; this.period.to='';
          this.loadConsumption(); },

      setCustom(){ if(this.period.from&&this.period.to){
          this.period.preset=''; this.loadConsumption(); } },

      periodQuery(){ if(this.period.preset) return 'period='+this.period.preset;
          return 'from='+this.period.from+'&to='+this.period.to; },

      async loadConsumption(){
          this.consLoading=true; this.chartBars=[]; this.chartSvg=''; this.chartMeterName='';
          try{
            const r=await fetch('/api/summary/consumption?'+this.periodQuery());
            const d=await r.json();
            this.consItems=d.items||[]; this.consTotal=d.consumption_kwh_total||0;
            this.consAnyUnknown=d.any_unknown||false;
          }catch(e){ console.error(e); }
          this.consLoading=false;
        },

      async loadHourly(devId){
          try{
            const r=await fetch('/api/meters/'+encodeURIComponent(devId)+'/hourly?'+this.periodQuery());
            const d=await r.json();
            this.chartMeterName=d.display_name||devId;
            this.chartBars=(d.items||[]).map(it=>({t:it.period_start,v:it.consumption_kwh}));
            this.renderChart();
          }catch(e){ console.error(e); }
        },

      renderChart(){
          const bars=this.chartBars.filter(b=>b.v!=null);
          if(!bars.length){ this.chartSvg='<div class="muted">Нет данных за период</div>'; return; }
          const W=Math.max(600,bars.length*14),H=200,pad=30;
          const max=Math.max(...bars.map(b=>b.v),0.001);
          const bw=(W-pad*2)/bars.length;
          let svg=`<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px">`;
          svg+=`<line x1="${pad}" y1="${H-pad}" x2="${W-pad}" y2="${H-pad}" stroke="var(--line)"/>`;
          bars.forEach((b,i)=>{
            const h=(b.v/max)*(H-pad*2),x=pad+i*bw,y=H-pad-h;
            svg+=`<rect class="bar" x="${x+1}" y="${y}" width="${Math.max(1,bw-2)}" height="${h}" `+
              `fill="var(--accent2)" rx="1"><title>${fmtTime(b.t)}: ${b.v.toFixed(3)} кВт·ч</title></rect>`;
          });
          svg+=`<text x="${pad}" y="14" fill="var(--txt2)" font-size="11">max ${max.toFixed(3)} кВт·ч</text>`;
          svg+=`</svg>`;
          this.chartSvg=svg;
        },

      async initReports(){
          // B2: было loadSettings() без await — loadVedomost() тут же читал
          // this.regMeters для колонки «Комментарий», на первом рендере она
          // была пустой.
          if(!this.regMeters.length) await this.loadSettings();
          if(this.rMode==='vedomost') this.loadVedomost();
          else if(this.rMode==='avail') this.loadAvail();
          else if(this.rMode==='balance') this.loadBalance();
          else if(this.rMode==='profile') this.loadProfile();
          else this.loadCompare();
        },

      _rPeriodQuery(preset, from_, to_){
          if(preset) return 'period='+preset;
          if(from_&&to_) return 'from='+from_+'&to='+to_;
          return 'period=this_month';
        },

      csvCell(v){
          if(v==null) return '';
          let s=String(v);
          if(/^[=+\-@\t\r]/.test(s)){ s = "'"+s; }
          return /[";\r\n]/.test(s) ? '"'+s.replace(/"/g,'""')+'"' : s;
        },

      async loadVedomost(){
          this.rLoading=true;
          const q=this._rPeriodQuery(this.rPeriod,this.rFrom,this.rTo);
          try{
            // Получаем notes из regMeters (уже загружены в настройках)
            const notesMap={};
            this.regMeters.forEach(m=>{ notesMap[m.device_id]=m.notes; });
            const r=await fetch('/api/summary/consumption?'+q);
            const d=await r.json();
            const items=d.items||[];
            this.rTotal=d.consumption_kwh_total||0;
            this.rPeriodLabel=d.period?
              (d.period.description||''):('');
            // Группируем по зонам
            const zones={};
            items.forEach(it=>{
              const z=it.group||'— Без зоны —';
              if(!zones[z]) zones[z]={name:z,items:[],total:0};
              zones[z].items.push({...it,notes:notesMap[it.device_id]||null});
              zones[z].total+=(it.consumption_kwh||0);
            });
            const rows=[];
            const named=Object.keys(zones).filter(k=>k!=='— Без зоны —').sort();
            const order=[...named,'— Без зоны —'].filter(k=>zones[k]);
            order.forEach(z=>{
              rows.push({_zone_sep:z,_zone_kwh:zones[z].total,_key:'z_'+z});
              zones[z].items.forEach((it,i)=>{
                rows.push({...it,_key:it.device_id+'_'+i,_zone_sep:null});
              });
            });
            this.vedomostRows=rows;
          }catch(e){ console.error(e); }
          this.rLoading=false;
        },

      downloadVedomost(){
          const lines=[];
          const pLabel=this.rPeriodLabel||this.rPeriod||'период';
          lines.push('\uFEFF'); // BOM для Excel
          lines.push(`wb-energy-meter — Ведомость расхода (${pLabel})`);
          lines.push('');
          lines.push('Зона;Счётчик;device_id;Комментарий;Расход кВт·ч;Качество');
          let curZone='';
          this.vedomostRows.forEach(r=>{
            if(r._zone_sep){ curZone=r._zone_sep; return; }
            const v=r.consumption_kwh!=null?r.consumption_kwh.toFixed(3):'';
            lines.push([this.csvCell(curZone),this.csvCell(r.display_name),this.csvCell(r.device_id),
              this.csvCell(r.notes||''),v,this.csvCell(r.quality||'')].join(';'));
          });
          lines.push('');
          lines.push(`;;ИТОГО;;${this.rTotal.toFixed(3)};`);
          const blob=new Blob([lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
          const a=document.createElement('a');
          a.href=URL.createObjectURL(blob);
          a.download=`vedomost_${this.rPeriod||'custom'}_${new Date().toISOString().slice(0,10)}.csv`;
          a.click();
        },

      async loadProfile(){
          if(!this.profileMeterId){ this.profileLoading=false; return; }
          this.profileLoading=true;
          const q=this._rPeriodQuery(this.profilePeriod,this.profileFrom,this.profileTo);
          try{
            const r=await fetch('/api/meters/'+encodeURIComponent(this.profileMeterId)+'/hourly?'+q);
            const d=await r.json();
            const items=d.items||[];
            this.profileHours=items.map(it=>({
              t:it.period_start, v:it.consumption_kwh, q:it.quality
            }));
            const vals=this.profileHours.filter(h=>h.v!=null).map(h=>h.v);
            this.profileTotal=vals.reduce((a,b)=>a+b,0);
            if(vals.length>0){
              const maxV=Math.max(...vals);
              // B5: ищем пик по объекту (единственная ссылка), а не по
              // значению — иначе при равных значениях подсвечивались бы все
              // совпавшие бары.
              const peak=this.profileHours.find(h=>h.v===maxV);
              this.profilePeak={v:maxV,label:peak?fmtHour(peak.t):'',item:peak||null};
            } else { this.profilePeak={v:0,label:'',item:null}; }
            this.renderProfileSvg();
          }catch(e){ console.error(e); }
          this.profileLoading=false;
        },

      renderProfileSvg(){
          const bars=this.profileHours.filter(b=>b.v!=null);
          if(!bars.length){ this.profileSvg='<div class="muted">Нет данных</div>'; return; }
          const W=Math.max(700,bars.length*12), H=220, padL=48, padR=12, padT=20, padB=30;
          const max=Math.max(...bars.map(b=>b.v),0.001);
          const bw=(W-padL-padR)/bars.length;
          let svg=`<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px">`;
          // Сетка
          for(let i=0;i<=4;i++){
            const y=padT+(H-padT-padB)*i/4;
            const val=(max*(4-i)/4);
            svg+=`<line x1="${padL}" y1="${y}" x2="${W-padR}" y2="${y}" stroke="var(--line)" stroke-dasharray="3,3"/>`;
            svg+=`<text x="${padL-4}" y="${y+4}" fill="var(--txt3)" font-size="10" text-anchor="end">${val.toFixed(2)}</text>`;
          }
          // Ось X
          svg+=`<line x1="${padL}" y1="${H-padB}" x2="${W-padR}" y2="${H-padB}" stroke="var(--line)"/>`;
          bars.forEach((b,i)=>{
            const h=(b.v/max)*(H-padT-padB);
            const x=padL+i*bw, y=H-padB-h;
            const color=(b===this.profilePeak.item)?'var(--warn)':'var(--accent2)';
            svg+=`<rect class="bar" x="${x+1}" y="${y}" width="${Math.max(1,bw-2)}" height="${h}" `+
              `fill="${color}" rx="1"><title>${fmtHour(b.t)}: ${b.v.toFixed(3)} кВт·ч</title></rect>`;
            // Метки оси X каждые 24 часа (или каждые 4 для коротких периодов)
            const step=bars.length>72?24:bars.length>24?4:1;
            if(i%step===0){
              const d=new Date(b.t*1000);
              const lbl=bars.length>72?
                `${String(d.getDate()).padStart(2,'0')}.${String(d.getMonth()+1).padStart(2,'0')}`:
                `${String(d.getHours()).padStart(2,'0')}:00`;
              svg+=`<text x="${x+bw/2}" y="${H-padB+14}" fill="var(--txt3)" `+
                `font-size="9" text-anchor="middle">${lbl}</text>`;
            }
          });
          // Подсветка пика
          if(this.profilePeak.v>0){
            svg+=`<text x="${W-padR}" y="${padT}" fill="var(--warn)" `+
              `font-size="10" text-anchor="end">▲ пик: ${this.profilePeak.v.toFixed(3)} кВт·ч (${this.profilePeak.label})</text>`;
          }
          svg+=`</svg>`;
          this.profileSvg=svg;
        },

      downloadProfile(){
          if(!this.profileHours.length) return;
          const lines=['\uFEFF','wb-energy-meter — Профиль нагрузки','',
            'Дата-час;Расход кВт·ч;Качество'];
          this.profileHours.forEach(h=>{
            lines.push([this.csvCell(fmtHour(h.t)),h.v!=null?h.v.toFixed(4):'',this.csvCell(h.q||'')].join(';'));
          });
          lines.push('');
          lines.push(`;ИТОГО;${this.profileTotal.toFixed(3)}`);
          const blob=new Blob([lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
          const a=document.createElement('a');
          a.href=URL.createObjectURL(blob);
          a.download=`profile_${this.profileMeterId}_${this.profilePeriod||'custom'}.csv`;
          a.click();
        },

      async loadCompare(){
          this.cmpLoading=true;
          try{
            const [da,db]=await Promise.all([
              fetch('/api/summary/consumption?period='+this.cmpPeriodA).then(r=>r.json()),
              fetch('/api/summary/consumption?period='+this.cmpPeriodB).then(r=>r.json()),
            ]);
            const labelOf=d=>(d.period&&d.period.description)||'период';
            this.cmpLabelA=labelOf(da); this.cmpLabelB=labelOf(db);
            const mapA={},mapB={};
            (da.items||[]).forEach(it=>{ mapA[it.device_id]=it; });
            (db.items||[]).forEach(it=>{ mapB[it.device_id]=it; });
            const ids=[...new Set([...Object.keys(mapA),...Object.keys(mapB)])];
            this.cmpRows=ids.map(id=>{
              const a=mapA[id]?.consumption_kwh??null;
              const b=mapB[id]?.consumption_kwh??null;
              const delta=(a!=null&&b!=null)?b-a:null;
              const pct=(delta!=null&&a!=null&&a>0)?(delta/a*100):null;
              return {
                device_id:id,
                display_name:(mapA[id]||mapB[id])?.display_name||id,
                group:(mapA[id]||mapB[id])?.group||'',
                a,b,delta,pct,
              };
            }).sort((x,y)=>(y.b||0)-(x.b||0));
            this.cmpTotalA=da.consumption_kwh_total||0;
            this.cmpTotalB=db.consumption_kwh_total||0;
          }catch(e){ console.error(e); }
          this.cmpLoading=false;
        },

      downloadCompare(){
          const lines=['\uFEFF','wb-energy-meter — Сравнение периодов','',
            `Счётчик;Зона;${this.cmpLabelA} кВт·ч;${this.cmpLabelB} кВт·ч;Δ кВт·ч;Δ %`];
          this.cmpRows.forEach(r=>{
            lines.push([this.csvCell(r.display_name),this.csvCell(r.group||''),
              r.a!=null?r.a.toFixed(3):'',
              r.b!=null?r.b.toFixed(3):'',
              r.delta!=null?r.delta.toFixed(3):'',
              r.pct!=null?r.pct.toFixed(1)+'%':''].join(';'));
          });
          lines.push('');
          lines.push([`ИТОГО`,'',this.cmpTotalA.toFixed(3),this.cmpTotalB.toFixed(3),
            (this.cmpTotalB-this.cmpTotalA).toFixed(3),
            this.cmpTotalA>0?((this.cmpTotalB-this.cmpTotalA)/this.cmpTotalA*100).toFixed(1)+'%':''].join(';'));
          const blob=new Blob([lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
          const a=document.createElement('a');
          a.href=URL.createObjectURL(blob);
          a.download=`compare_${this.cmpPeriodA}_vs_${this.cmpPeriodB}.csv`;
          a.click();
        },

      balancePeriod:'this_month',

      balanceLoading:false,

      balanceData:null,

      async loadBalance(){
          this.balanceLoading=true;
          try{
            const r=await fetch('/api/reports/balance?period='+this.balancePeriod);
            this.balanceData=await r.json();
          }catch(e){ console.error(e); this.balanceData=null; }
          this.balanceLoading=false;
        },

      downloadBalance(){
          if(!this.balanceData) return;
          const d=this.balanceData;
          const lines=['\uFEFF',
            'wb-energy-meter — Баланс электроэнергии',
            `Период: ${d.period&&d.period.description||this.balancePeriod}`,
            '',
            '=== ВВОД ===',
            'Счётчик;device_id;Зона;Расход кВт·ч;Качество',
          ];
          (d.input.meters||[]).forEach(m=>{
            lines.push([this.csvCell(m.display_name),this.csvCell(m.device_id),this.csvCell(m.group||''),
              m.consumption_kwh!=null?m.consumption_kwh.toFixed(3):'',
              this.csvCell(m.quality||'')].join(';'));
          });
          lines.push([';ИТОГО ВВОД;;;'+d.input.total_kwh.toFixed(3)].join(''));
          lines.push('');
          lines.push('=== ПОТРЕБИТЕЛИ ===');
          lines.push('Счётчик;device_id;Зона;Расход кВт·ч;Качество');
          (d.consumer.meters||[]).forEach(m=>{
            lines.push([this.csvCell(m.display_name),this.csvCell(m.device_id),this.csvCell(m.group||''),
              m.consumption_kwh!=null?m.consumption_kwh.toFixed(3):'',
              this.csvCell(m.quality||'')].join(';'));
          });
          lines.push([';ИТОГО ПОТРЕБИТЕЛИ;;;'+d.consumer.total_kwh.toFixed(3)].join(''));
          lines.push('');
          lines.push('=== ИТОГ ===');
          lines.push(`Ввод кВт·ч;;${d.input.total_kwh.toFixed(3)}`);
          lines.push(`Потребители кВт·ч;;${d.consumer.total_kwh.toFixed(3)}`);
          lines.push(`Небаланс кВт·ч;;${d.imbalance_kwh.toFixed(3)}`);
          lines.push(`Небаланс %;;${d.imbalance_pct!=null?d.imbalance_pct.toFixed(2)+'%':'—'}`);
          const blob=new Blob([lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
          const a=document.createElement('a');
          a.href=URL.createObjectURL(blob);
          a.download=`balance_${this.balancePeriod}_${new Date().toISOString().slice(0,10)}.csv`;
          a.click();
        }
    };
  });
})();

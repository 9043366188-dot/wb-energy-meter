// reports-v2.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function reportsV2Part() {
    return {
      rv2Inited:false,

      rv2Dimension:'branch',

      rv2ScopeIds:[],

      rv2From:null,

      rv2To:null,

      rv2CompositionMode:'as_was',

      rv2CompareEnabled:false,

      rv2CompareFrom:null,

      rv2CompareTo:null,

      rv2Loading:false,

      rv2Error:null,

      rv2Result:null,

      rv2Points:[],

      rv2Groups:[],

      rv2BalanceScopes:[],

      rv2OptionsLoaded:false,

      async openReportsV2Tab(){
          if(!this.rv2From || !this.rv2To){
            const d=this._ovDefaultDates();  // тот же дефолт периода, что и у "Итог объекта за период"
            this.rv2From=d.from; this.rv2To=d.to;
          }
          await this._loadOvPointNames();  // имена точек для колонки "Состав" и CSV
          if(!this.rv2OptionsLoaded){
            this.rv2OptionsLoaded=true;
            await this._loadRv2Options();
          }
        },

      async _loadRv2Options(){
          // Списки для пикера состава (точка/группа/граница баланса) —
          // необязательно для самого расчёта, при ошибке пикеры просто пустые.
          try{
            const [pr, gr, br] = await Promise.all([
              fetch('/api/v2/points'), fetch('/api/v2/groups'), fetch('/api/v2/balance-scopes')]);
            this.rv2Points = pr.ok ? await pr.json() : [];
            this.rv2Groups = gr.ok ? await gr.json() : [];
            this.rv2BalanceScopes = br.ok ? await br.json() : [];
          }catch(e){ /* необязательно */ }
        },

      rv2ScopeOptions(){
          if(this.rv2Dimension==='point') return this.rv2Points.map(p=>({id:p.id, label:(p.name||p.code)}));
          if(this.rv2Dimension==='group') return this.rv2Groups.map(g=>({id:g.id, label:g.name}));
          if(this.rv2Dimension==='balance_scope') return this.rv2BalanceScopes.map(s=>({id:s.id, label:s.name}));
          return [];
        },

      async runRv2Query(){
          if(!this.rv2From || !this.rv2To) return;
          if(this.rv2Dimension!=='branch' && this.rv2ScopeIds.length===0){
            this.rv2Error='Выберите хотя бы один элемент состава'; this.rv2Result=null; return;
          }
          this.rv2Loading=true; this.rv2Error=null;
          try{
            const tz=(Intl.DateTimeFormat().resolvedOptions().timeZone)||'UTC';
            const body={
              dimension:this.rv2Dimension, from:this.rv2From, to:this.rv2To, timezone:tz,
              composition_mode:this.rv2CompositionMode,
            };
            if(this.rv2Dimension!=='branch') body.scope_ids=this.rv2ScopeIds.map(Number);
            if(this.rv2CompareEnabled && this.rv2CompareFrom && this.rv2CompareTo){
              body.compare={from:this.rv2CompareFrom, to:this.rv2CompareTo};
            }
            const r=await fetch('/api/v2/reports/query', {
              method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
            const d=await r.json();
            if(!r.ok) throw new Error(d.message||`HTTP ${r.status}`);
            this.rv2Result=d;
          }catch(e){
            this.rv2Error=`Не удалось получить отчёт: ${e.message}`;
            this.rv2Result=null;
          }finally{
            this.rv2Loading=false;
          }
        },

      downloadRv2Csv(){
          // A45: csvCell() уже нейтрализует префиксы формул (=+-@) — те же
          // правила, что и в существующих выгрузках v1. Числа НЕ идут через
          // csvCell (пишутся как есть, округлённые до 3 знаков — то же
          // округление, что и на экране, см. таблицу выше).
          if(!this.rv2Result) return;
          const d=this.rv2Result;
          const hasCmp = !!d.compare_period;
          const r3=v=>v!=null?Math.round(v*1000)/1000:'';
          const head=['Срез','ID','Название','Точки состава','Значение','Известная частичная сумма',
            'Единица','Доступность','Флаги качества','Ожидалось часов','Валидно часов',
            'С','По','Часовой пояс','Ревизия конфигурации','Режим состава','Конфликт'];
          if(hasCmp) head.push('Сравнение: с','Сравнение: по','Значение сравнения','Изменение',
            'Изменение %','Состав изменился','Конфликт сравнения');
          const lines=['﻿', `wb-energy-meter — Отчёт v2 (${d.dimension}, состав: ${d.composition_mode})`,
            '', head.join(';')];
          (d.rows||[]).forEach(row=>{
            const res=row.result;
            const names=(row.member_point_ids||[]).map(id=>this.ovPointName(id)).join(', ');
            const cells=[
              this.csvCell(row.dimension), row.id, this.csvCell(row.name), this.csvCell(names),
              r3(res?res.value:null), r3(res?res.known_value:null),
              this.csvCell(res?res.unit:''), this.csvCell(res?res.availability:''),
              this.csvCell(res?(res.quality_flags||[]).join('|'):''),
              res?res.expected_count:'', res?res.valid_count:'',
              this.csvCell(d.period.from), this.csvCell(d.period.to), this.csvCell(d.period.timezone),
              d.configuration_revision_id, this.csvCell(d.composition_mode),
              this.csvCell(row.conflict_reason||''),
            ];
            if(hasCmp){
              const cr=row.compare_result;
              cells.push(
                this.csvCell(d.compare_period.from), this.csvCell(d.compare_period.to),
                r3(cr?cr.value:null), r3(row.delta_value), row.delta_percentage!=null?row.delta_percentage:'',
                row.composition_changed===true?'да':(row.composition_changed===false?'нет':''),
                this.csvCell(row.compare_conflict_reason||''),
              );
            }
            lines.push(cells.join(';'));
          });
          const blob=new Blob([lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
          const a=document.createElement('a');
          a.href=URL.createObjectURL(blob);
          a.download=`report_v2_${d.dimension}_${new Date().toISOString().slice(0,10)}.csv`;
          a.click();
        }
    };
  });
})();

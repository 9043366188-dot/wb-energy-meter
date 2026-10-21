// plan-background.js — замена подложки плана этажа с предпросмотром, список прежних подложек
// и возврат старой (партия 14, A30).
//
// Порядок: человек выбирает файл → браузер при необходимости уменьшает его (как при создании
// плана) → сервер БЕЗ записи отвечает, во что превратится план (`POST /image/preview`:
// `compat` = same | scalable | reanchor) → окно показывает картинку с точками-значками и
// даёт выбрать, что делать со значками → только после «Заменить» уходит `POST /image`.
// Обрезанный/повёрнутый фон (`reanchor`) значки сам не двигает: пересчитать их пропорционально
// нельзя, их перепривязывают вручную. Прежние подложки сервер хранит (plan_image_versions):
// «Версии подложки…» показывает их и возвращает любую.
//
// Файл, который выбрал человек, и адрес его превью держим ВНЕ реактивности Alpine: FormData
// принимает только настоящий Blob/File, а Proxy Alpine его не пропускает.
let _pbgBlob=null;     // {blob, name, resized}
let _pbgUrl=null;      // object URL превью новой картинки (освобождаем при закрытии)

(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function planBackgroundPart() {
    return {
      // null | 'preview' (замена или возврат) | 'versions' (список прежних подложек)
      pbgDialog:null,
      pbgBusy:false,
      // {kind:'new'|'restore', versionId, info, url} — info: ответ /image/preview или строка списка версий
      pbgPreview:null,
      pbgMode:'keep',
      pbgVersions:[],

      // ---------------- выбор файла ----------------
      async pbgPick(ev){
          const input=ev && ev.target;
          const f=(input && input.files && input.files[0]) || null;
          const pc=this.planV3Current;
          if(!f || !pc || pc.plan_kind!=='floor'){ if(input) input.value=''; return; }
          this.planV3BgBusy=true;
          try{
            const fit=await _planV3FitImage(f);
            if(fit.error){ this.showToast(fit.error, false); return; }
            const fd=new FormData();
            fd.append('file', fit.blob, fit.name);
            const r=await fetch('/api/v2/plans/'+pc.id+'/image/preview', {method:'POST', body:fd});
            const d=await r.json();
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            this._pbgRevoke();
            _pbgBlob=fit;
            _pbgUrl=URL.createObjectURL(fit.blob);
            this._pbgOpenPreview({kind:'new', versionId:null, info:d, url:_pbgUrl});
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
          finally{ this.planV3BgBusy=false; if(input) input.value=''; }
        },

      _pbgRevoke(){
          if(_pbgUrl){ try{ URL.revokeObjectURL(_pbgUrl); }catch(e){} }
          _pbgUrl=null; _pbgBlob=null;
        },

      _pbgOpenPreview(p){
          this.pbgPreview=p;
          // пропорциональный пересчёт — по умолчанию там, где он возможен и что-то меняет
          this.pbgMode=(p.info.compat==='scalable') ? 'scale' : 'keep';
          this.pbgDialog='preview';
        },

      pbgClose(){
          this.pbgDialog=null; this.pbgPreview=null; this.pbgBusy=false;
          this._pbgRevoke();
        },

      // ---------------- версии ----------------
      async pbgOpenVersions(){
          const pc=this.planV3Current;
          if(!pc || pc.plan_kind!=='floor') return;
          this.planV3BgBusy=true;
          try{
            const r=await fetch('/api/v2/plans/'+pc.id+'/image-versions');
            const d=await r.json();
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            this.pbgVersions=d;
            this.pbgDialog='versions';
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
          finally{ this.planV3BgBusy=false; }
        },

      pbgVersionUrl(v){
          return '/api/v2/plans/'+this.planV3Current.id+'/image?version='+v.id;
        },

      pbgVersionTitle(v){
          const when=v.created_at ? new Date(v.created_at*1000).toLocaleString('ru-RU') : '';
          return v.image_width+'×'+v.image_height+' px'+(when ? ' · '+when : '');
        },

      pbgPickVersion(v){
          this._pbgRevoke();
          this._pbgOpenPreview({
            kind:'restore', versionId:v.id, url:this.pbgVersionUrl(v),
            info:{compat:v.compat, scale_x:v.scale_x, scale_y:v.scale_y,
                  old:v.old, new:v.new, items_total:v.items_total,
                  items_outside_if_kept:v.items_outside_if_kept}});
        },

      // ---------------- предпросмотр ----------------
      // Точки значков в картинке нового фона (в долях её ширины и высоты). При пересчёте
      // значок остаётся на том же месте рисунка, при «оставить на месте» — на тех же пикселях,
      // и может оказаться за краем картинки (out) — таких показываем красным у края.
      pbgDots(){
          const p=this.pbgPreview;
          if(!p || !p.info || !p.info.old || !this.planV3Current) return [];
          const info=p.info, scale=this.pbgMode==='scale';
          const dots=[];
          (this.planV3Current.items||[]).forEach(it=>{
            const g=it.geometry;
            if(!g || typeof g.x!=='number' || typeof g.y!=='number') return;
            const fx=scale ? g.x/info.old.width : g.x/info.new.width;
            const fy=scale ? g.y/info.old.height : g.y/info.new.height;
            const out=fx<0 || fx>1 || fy<0 || fy>1;
            dots.push({id:it.id, out, left:Math.min(1,Math.max(0,fx))*100, top:Math.min(1,Math.max(0,fy))*100});
          });
          return dots;
        },

      // Сколько значков окажется за краем при выбранном режиме.
      pbgOutsideCount(){
          const p=this.pbgPreview;
          if(!p || !p.info) return 0;
          return this.pbgMode==='scale' ? 0 : (p.info.items_outside_if_kept||0);
        },

      pbgNote(){
          const p=this.pbgPreview;
          if(!p || !p.info) return '';
          const i=p.info;
          const dims=i.old.width+'×'+i.old.height+' → '+i.new.width+'×'+i.new.height+' px';
          if(i.compat==='same'){
            return 'Размеры те же ('+i.old.width+'×'+i.old.height+' px): значки остаются на своих местах.';
          }
          if(i.compat==='scalable'){
            return 'Другое разрешение ('+dims+'), соотношение сторон то же. Значки можно пересчитать '
              +'пропорционально — они останутся на тех же местах рисунка.';
          }
          return 'Соотношение сторон изменилось ('+dims+'): фон обрезан, повёрнут или это другой чертёж. '
            +'Автоматически пересчитать значки нельзя — они останутся на прежних пикселях, и их нужно '
            +'перепривязать вручную. Прежнюю подложку можно будет вернуть в «Версии подложки…».';
        },

      // ---------------- применить ----------------
      async pbgApply(){
          const p=this.pbgPreview, pc=this.planV3Current;
          if(!p || !pc || this.pbgBusy) return;
          const mode=(p.info.compat==='scalable') ? this.pbgMode : 'keep';
          this.pbgBusy=true;
          try{
            let r;
            if(p.kind==='new'){
              if(!_pbgBlob){ this.pbgClose(); return; }
              const fd=new FormData();
              fd.append('file', _pbgBlob.blob, _pbgBlob.name);
              fd.append('mode', mode);
              r=await fetch('/api/v2/plans/'+pc.id+'/image', {method:'POST', body:fd});
            }else{
              r=await fetch('/api/v2/plans/'+pc.id+'/image/restore', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({version_id:p.versionId, mode})});
            }
            const d=await r.json();
            if(!r.ok){ this.showToast(this._apiErrorMessage(d, r.status), false); return; }
            const outside=mode==='scale' ? 0 : (p.info.items_outside_if_kept||0);
            const note=(p.kind==='new') ? _planV3ResizedNote(_pbgBlob) : '';
            const verb=(p.kind==='new') ? 'Подложка заменена.' : 'Прежняя подложка возвращена.';
            this.pbgClose();
            await this.selectPlanV3(pc.id);
            let msg;
            if(mode==='scale') msg=verb+' Значки пересчитаны пропорционально.';
            else if(p.info.compat==='reanchor') msg=verb+' Значки не двигались — перепривязайте их вручную'
              +(outside ? ' (за краем картинки: '+outside+')' : '')+'.';
            else msg=verb+' Значки остались на прежних пикселях картинки — проверьте, что они стоят где нужно.';
            this.showToast(msg+note, true);
            if(outside) { await this.$nextTick(); this._pbgFitWithItems(); }
          }catch(e){ this.showToast('Ошибка сети: '+e, false); }
          finally{ this.pbgBusy=false; }
        },

      // После замены без пересчёта часть значков может лежать за краем картинки: показываем
      // картинку вместе со всеми значками, чтобы их было видно и можно было перетащить.
      _pbgFitWithItems(){
          const pc=this.planV3Current;
          if(!_planV3Map || !pc) return;
          const [W,H]=_planEffectiveDims(pc);
          if(!W || !H) return;
          let b=L.latLngBounds([[0,0],[H,W]]);
          (pc.items||[]).forEach(it=>{
            const g=it.geometry;
            if(g && typeof g.x==='number' && typeof g.y==='number') b.extend(_planXyToLatLng(g.x, g.y, H));
          });
          try{ _planV3Map.fitBounds(b.pad(0.05), {animate:false}); }catch(e){}
        },
    };
  });
})();

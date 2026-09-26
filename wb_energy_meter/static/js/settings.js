// settings.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
// ---- модульные переменные/хелперы только этого экрана ----
const UPD_ACTIVE_STATES=['starting','downloading','installing','verifying','rolling_back'];

(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function settingsPart() {
    return {
      updChecking:false,

      updChecked:false,

      updCurrent:null,

      updRemote:null,

      updAvailable:false,

      updCheckedAt:null,

      updCheckError:'',

      updAllowFromUi:true,

      updConfirmOpen:false,

      updStatus:{state:'idle'},

      updResultHidden:false,

      _updPollFailStartedAt:null,

      _updPollTimer:null,

      uptimeItems:[],

      uptimeAllowEdit:false,

      uptimePending:[],

      uptimeSha:null,

      uptimeConfigError:'',

      uptimeBusy:false,

      uptimeOpenId:null,

      uptimeRestartConfirm:false,

      settingsLoading:false,

      regMeters:[],

      unregistered:[],

      allGroups:[],

      zones:[],

      zoneEditId:null,

      zoneEditName:'',

      zoneNewName:'',

      confirmZone:null,

      mergeConfirm:null,

      scanLoading:false,

      editingId:null,

      editName:'',

      editGroup:'',

      editGroupNew:'',

      editNotes:'',

      editRole:'consumer',

      get updIsActive(){
          return !!(this.updStatus && UPD_ACTIVE_STATES.includes(this.updStatus.state));
        },

      get updIsTerminal(){
          return !!(this.updStatus && ['success','rolled_back','failed'].includes(this.updStatus.state));
        },

      updStateLabel(s){
          return {starting:'Запуск обновления…', downloading:'Скачивание…',
                  installing:'Установка…', verifying:'Проверка после установки…',
                  rolling_back:'Откат на предыдущую версию…'}[s] || (s||'');
        },

      updShortSha(sha){ return sha ? String(sha).slice(0,7) : '—'; },

      async initUpdatePanel(){
          // Опрос статуса — при каждом открытии «Настроек»: если обновление
          // шло, когда пользователь закрыл вкладку, он должен увидеть
          // результат. Это ЛОКАЛЬНЫЙ опрос (файл статуса), не GitHub —
          // "автопроверки нет" (§1 ТЗ) касается только checkUpdate().
          await this.pollUpdateStatusOnce();
        },

      async checkUpdate(){
          this.updChecking=true; this.updCheckError='';
          try{
            const r=await fetch('/api/update/check');
            const d=await r.json();
            if(!r.ok){ this.updCheckError=d.error||'Ошибка проверки обновлений'; this.updChecking=false; return; }
            this.updCurrent=d.current; this.updRemote=d.remote;
            this.updAvailable=!!d.update_available; this.updAllowFromUi=!!d.allow_from_ui;
            this.updChecked=true; this.updCheckedAt=Date.now()/1000;
          }catch(e){ this.updCheckError='Не удалось связаться с сервисом: '+e; }
          this.updChecking=false;
        },

      async startUpdateNow(){
          this.updConfirmOpen=false;
          try{
            const r=await fetch('/api/update/start',
              {method:'POST', headers:{'Content-Type':'application/json'},
               body:JSON.stringify({commit:this.updRemote&&this.updRemote.commit})});
            const d=await r.json().catch(()=>({}));
            if(!r.ok){ this.showToast(d.error||'Не удалось запустить обновление',false); return; }
            this.updResultHidden=false; this.updStatus=d;
            this._updPollFailStartedAt=null;
            this._ensureUpdatePolling();
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        },

      async pollUpdateStatusOnce(){
          try{
            const r=await fetch('/api/update/status');
            if(!r.ok) throw new Error('HTTP '+r.status);
            const d=await r.json();
            this._updPollFailStartedAt=null;
            const prevState=this.updStatus&&this.updStatus.state;
            this.updStatus=d;
            if(prevState!==d.state) this.updResultHidden=false;
            if(UPD_ACTIVE_STATES.includes(d.state)){ this._ensureUpdatePolling(); }
            else { this._stopUpdatePolling(); }
          }catch(e){
            // Сервис перезапускается — нормальная часть обновления, не ошибка.
            // Сдаёмся только после 120 секунд подряд неудачных попыток (§4.6 п.6).
            if(!this._updPollFailStartedAt) this._updPollFailStartedAt=Date.now();
            this._ensureUpdatePolling();
            if(Date.now()-this._updPollFailStartedAt>120000){
              this._stopUpdatePolling();
              this.updStatus={state:'unreachable'};
            }
          }
        },

      _ensureUpdatePolling(){
          if(this._updPollTimer) return;
          this._updPollTimer=setInterval(()=>this.pollUpdateStatusOnce(),2000);
        },

      _stopUpdatePolling(){
          if(this._updPollTimer){ clearInterval(this._updPollTimer); this._updPollTimer=null; }
        },

      async loadUptimeSummary(){
          try{
            const r=await fetch('/api/uptime-channel/summary');
            const d=await r.json();
            this.uptimeItems=d.items||[];
            this.uptimeAllowEdit=!!d.allow_edit;
            this.uptimePending=d.pending||[];
            this.uptimeSha=d.sha256||null;
            this.uptimeConfigError=d.config_error||'';
          }catch(e){ console.error(e); }
        },

      uptimeRow(devId){
          return (this.uptimeItems||[]).find(u=>u.device_id===devId)||null;
        },

      uptimeStateOf(devId){
          const u=this.uptimeRow(devId); return u? u.state : 'unknown';
        },

      uptimeDetailOf(devId){
          const u=this.uptimeRow(devId);
          return u? (u.detail||'') : 'Состояние канала Uptime ещё не проверено.';
        },

      uptimeLabel(st){ return {
          ok:'опрашивается', disabled:'не включён', not_in_config:'не включён',
          stale:'нет данных', device_not_found:'не в конфиге',
          unknown:'не проверено'}[st]||'не проверено';
        },

      uptimeColor(st){ return {
          ok:'var(--ok)', disabled:'var(--warn)', not_in_config:'var(--warn)',
          stale:'var(--err)', device_not_found:'var(--txt3)',
          unknown:'var(--txt3)'}[st]||'var(--txt3)';
        },

      uptimeBadgeStyle(st){
          const c=this.uptimeColor(st);
          return `background:${c}22;color:${c};border:1px solid ${c}55`;
        },

      get uptimeProblems(){
          return (this.uptimeItems||[]).filter(u=>u.state!=='ok');
        },

      async enableUptime(devId){
          if(this.uptimeBusy) return;
          this.uptimeBusy=true;
          try{
            const r=await fetch('/api/wb-config/enable-uptime',
              {method:'POST', headers:{'Content-Type':'application/json'},
               body:JSON.stringify({device_id:devId, sha256:this.uptimeSha})});
            const d=await r.json().catch(()=>({}));
            if(!r.ok){
              this.showToast(d.error||'Не удалось включить канал Uptime',false);
            }else{
              this.uptimePending=d.pending||this.uptimePending;
              if(d.sha256) this.uptimeSha=d.sha256;
              this.showToast(d.already_enabled
                ? 'Канал Uptime уже включён в конфиге'
                : 'Канал Uptime включён, перезапустите опрос', true);
            }
          }catch(e){ this.showToast('Ошибка: '+e,false); }
          this.uptimeBusy=false;
          await this.loadUptimeSummary();
        },

      async restartDriverNow(){
          this.uptimeRestartConfirm=false;
          if(this.uptimeBusy) return;
          this.uptimeBusy=true;
          try{
            const r=await fetch('/api/wb-config/restart-driver',{method:'POST'});
            const d=await r.json().catch(()=>({}));
            if(!r.ok){
              this.showToast((d.error||'Драйвер не перезапустился')+
                (d.rollback?(' · '+d.rollback):''), false);
            }else{
              this.uptimePending=[];
              this.showToast('Драйвер wb-mqtt-serial перезапущен',true);
            }
          }catch(e){ this.showToast('Ошибка: '+e,false); }
          this.uptimeBusy=false;
          await this.loadUptimeSummary();
        },

      async runMigrateLegacy(){
          this.migrateLegacyConfirm=false;
          this.migrateLegacyBusy=true;
          this.migrateLegacyError=null;
          this.migrateLegacyResult=null;
          try{
            const r=await fetch('/api/v2/admin/migrate-legacy', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body:JSON.stringify({confirm:true}),
            });
            const d=await r.json().catch(()=>({}));
            if(!r.ok){
              this.migrateLegacyError=(d.message||d.error||'Ошибка переноса');
              if(this.showToast) this.showToast(this.migrateLegacyError, false);
              return;
            }
            this.migrateLegacyResult=d;
            if(this.showToast) this.showToast('Перенос завершён: '+(d.migrated_points||[]).length+' точек', true);
          }catch(e){
            this.migrateLegacyError='Ошибка сети: '+e;
            if(this.showToast) this.showToast(this.migrateLegacyError, false);
          }finally{
            this.migrateLegacyBusy=false;
          }
        },

      async loadSettings(){
          this.settingsLoading=true;
          try{
            const [rm, gr] = await Promise.all([
              fetch('/api/registry/meters').then(r=>r.json()),
              fetch('/api/registry/groups').then(r=>r.json()),
            ]);
            this.regMeters=rm.items||[];
            this.zones=gr.groups||[];
            this.allGroups=(gr.groups||[]).map(g=>g.name);
          }catch(e){ console.error(e); }
          this.settingsLoading=false;
          this.loadUptimeSummary();
          this.scanUnregistered();
        },

      async reloadGroups(){
          try{
            const gr=await fetch('/api/registry/groups').then(r=>r.json());
            this.zones=gr.groups||[];
            this.allGroups=(gr.groups||[]).map(g=>g.name);
          }catch(e){ console.error(e); }
        },

      async createZone(){
          const name=this.zoneNewName.trim();
          if(!name) return;
          try{
            const r=await fetch('/api/registry/groups',
              {method:'POST',headers:{'Content-Type':'application/json'},
               body:JSON.stringify({name})});
            if(!r.ok){const e=await r.json();this.showToast(e.error||'Ошибка',false);return;}
            this.zoneNewName='';
            this.showToast('Зона создана');
            await this.reloadGroups();
          }catch(e){this.showToast('Ошибка: '+e,false);}
        },

      startZoneEdit(z){this.zoneEditId=z.id;this.zoneEditName=z.name;},

      async saveZoneEdit(id, merge){
          const name=this.zoneEditName.trim();
          if(!name){this.zoneEditId=null;return;}
          try{
            const body={name}; if(merge) body.merge=true;
            const r=await fetch('/api/registry/groups/'+id,
              {method:'PATCH',headers:{'Content-Type':'application/json'},
               body:JSON.stringify(body)});
            if(r.status===409){
              // Зона с таким именем уже есть — предлагаем объединить (A3/A5)
              const e=await r.json();
              this.mergeConfirm={groupId:id, name, existingId:e.existing_id};
              return;
            }
            if(!r.ok){const e=await r.json();this.showToast(e.error||'Ошибка',false);return;}
            this.zoneEditId=null; this.mergeConfirm=null;
            this.showToast(merge?'Зоны объединены':'Зона переименована');
            await this.reloadGroups();
            await this.loadSettings();
            await this.loadStatus();
          }catch(e){this.showToast('Ошибка: '+e,false);}
        },

      async confirmMergeZones(){
          if(!this.mergeConfirm) return;
          const groupId=this.mergeConfirm.groupId;
          this.mergeConfirm=null;
          await this.saveZoneEdit(groupId, true);
        },

      async changeZoneColor(z, color){
          try{
            const r=await fetch('/api/registry/groups/'+z.id,
              {method:'PATCH',headers:{'Content-Type':'application/json'},
               body:JSON.stringify({color})});
            if(!r.ok){const e=await r.json();this.showToast(e.error||'Ошибка',false);return;}
            await this.reloadGroups();
          }catch(e){this.showToast('Ошибка: '+e,false);}
        },

      askDeleteZone(z){this.confirmZone=z;},

      async confirmDeleteZone(){
          const z=this.confirmZone; this.confirmZone=null;
          try{
            const r=await fetch('/api/registry/groups/'+z.id,{method:'DELETE'});
            if(!r.ok){const e=await r.json();this.showToast(e.error||'Ошибка',false);return;}
            this.showToast('Зона удалена, счётчики переведены в "без зоны"');
            await this.reloadGroups();
            await this.loadSettings();
            await this.loadStatus();
          }catch(e){this.showToast('Ошибка: '+e,false);}
        },

      async scanUnregistered(){
          this.scanLoading=true;
          try{
            const r=await fetch('/api/meters/unregistered');
            const d=await r.json();
            this.unregistered=d.items||[];
          }catch(e){ console.error(e); }
          this.scanLoading=false;
        },

      startEdit(m){
          this.editingId=m.device_id;
          this.editName=m.display_name;
          this.editGroup=m.group_name||'';
          this.editGroupNew='';
          this.editNotes=m.notes||'';
          this.editRole=m.role||'consumer';
        },

      async saveEdit(deviceId){
          let group=this.editGroup;
          if(group==='__new__'){
            group=(this.editGroupNew||'').trim();
            if(!group){ this.showToast('Введите название новой зоны', false); return; }
          }
          const body={display_name:this.editName.trim(),group,notes:this.editNotes.trim()};
          // Роль сохраняем отдельным запросом если изменилась
          const origRole=(this.regMeters.find(m=>m.device_id===deviceId)||{}).role||'consumer';
          const roleChanged=this.editRole!==origRole;
          try{
            const r=await fetch('/api/registry/meters/'+encodeURIComponent(deviceId),
              {method:'PATCH',headers:{'Content-Type':'application/json'},
               body:JSON.stringify(body)});
            if(!r.ok){ const e=await r.json(); this.showToast(e.error||'Ошибка',false); return; }
            this.editingId=null;
            if(roleChanged) await this.saveRole(deviceId, this.editRole);
            else { this.showToast('Сохранено'); await this.loadSettings(); await this.loadStatus(); }
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        },

      availPeriod:'last_30d',

      availLoading:false,

      availItems:[],

      availSelected:null,

      availDetail:{},

      async loadAvail(){
          this.availLoading=true; this.availSelected=null; this.availDetail={};
          try{
            const r=await fetch('/api/availability/summary?period='+this.availPeriod);
            const d=await r.json();
            this.availItems=d.items||[];
          }catch(e){ console.error(e); }
          this.availLoading=false;
        },

      async loadAvailDetail(deviceId){
          try{
            const r=await fetch('/api/meters/'+encodeURIComponent(deviceId)+
              '/availability?period='+this.availPeriod);
            const d=await r.json();
            this.availDetail={...this.availDetail,[deviceId]:d};
          }catch(e){ console.error(e); }
        },

      downloadAvail(){
          if(!this.availItems.length) return;
          const lines=['\uFEFF','wb-energy-meter — Доступность','',
            'Счётчик;device_id;Зона;Роль;Доступность %;Недоступность с;Инцидентов'];
          this.availItems.forEach(r=>{
            lines.push([this.csvCell(r.display_name),this.csvCell(r.device_id),this.csvCell(r.group||''),
              this.csvCell(this.roleLabel(r.role)),
              r.availability_pct.toFixed(2)+'%',
              r.unavailable_s||0,
              r.incidents||0].join(';'));
          });
          const blob=new Blob([lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
          const a=document.createElement('a');
          a.href=URL.createObjectURL(blob);
          a.download=`availability_${this.availPeriod}.csv`;
          a.click();
        },

      roleLabel(r){return {input:'Ввод',consumer:'Потребитель',other:'Прочее'}[r]||r||'—';},

      roleColor(r){return {input:'var(--ok)',consumer:'var(--accent2)',other:'var(--txt3)'}[r]||'var(--txt3)';},

      async saveRole(deviceId, role){
          try{
            const r=await fetch('/api/registry/meters/'+encodeURIComponent(deviceId)+'/role',
              {method:'PATCH',headers:{'Content-Type':'application/json'},
               body:JSON.stringify({role})});
            if(!r.ok){const e=await r.json();this.showToast(e.error||'Ошибка',false);return;}
            this.showToast('Роль изменена');
            await this.loadSettings(); await this.loadStatus();
          }catch(e){this.showToast('Ошибка: '+e,false);}
        }
    };
  });
})();

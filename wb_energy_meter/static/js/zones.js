// zones.js — извлечено из index.html партией 9 (F5, docs/TZ-batch9-split-frontend.md).
// Механический перенос кода, поведение не менялось.
(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function zonesPart() {
    return {
      selectedMeters:[],

      bulkGroup:'',

      addingId:null,

      addName:'',

      addGroup:'',

      addGroupNew:'',

      addNotes:'',

      confirmTarget:null,

      get allSelected(){
          return this.regMeters.length>0 && this.selectedMeters.length===this.regMeters.length;
        },

      toggleSelectAll(ev){
          this.selectedMeters = ev.target.checked ? this.regMeters.map(m=>m.device_id) : [];
        },

      async applyBulkGroup(){
          const ids=[...this.selectedMeters];
          if(!ids.length) return;
          const group=this.bulkGroup;
          let ok=0;
          for(const id of ids){
            try{
              const r=await fetch('/api/registry/meters/'+encodeURIComponent(id),
                {method:'PATCH',headers:{'Content-Type':'application/json'},
                 body:JSON.stringify({group})});
              if(r.ok) ok++;
            }catch(e){ /* пропускаем, считаем ниже по ok */ }
          }
          this.selectedMeters=[];
          const zoneLabel=group?('«'+group+'»'):'«Без зоны»';
          this.showToast(`Перемещено ${ok} из ${ids.length} счётчиков в зону ${zoneLabel}`, ok===ids.length);
          await this.loadSettings();
          await this.loadStatus();
        },

      startAdd(u){
          this.addingId=u.device_id;
          this.addName=u.mqtt_name||u.device_id;
          this.addGroup=''; this.addGroupNew='';
        },

      async confirmAdd(deviceId){
          const name=this.addName.trim()||deviceId;
          let group=this.addGroup;
          if(group==='__new__') group=(this.addGroupNew||'').trim();
          const body={device_id:deviceId, display_name:name,
                      group:group||null,
                      notes:this.addNotes.trim()||null};
          try{
            const r=await fetch('/api/registry/meters',
              {method:'POST',headers:{'Content-Type':'application/json'},
               body:JSON.stringify(body)});
            if(!r.ok){ const e=await r.json(); this.showToast(e.error||'Ошибка',false); return; }
            this.addingId=null; this.addNotes=''; this.addGroup=''; this.addGroupNew='';
            this.showToast('Счётчик добавлен');
            await this.loadSettings();
            await this.loadStatus();
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        },

      askDelete(m){ this.confirmTarget=m; },

      async confirmDelete(){
          const did=this.confirmTarget.device_id;
          this.confirmTarget=null;
          try{
            const r=await fetch('/api/registry/meters/'+encodeURIComponent(did),
              {method:'DELETE'});
            if(!r.ok){ const e=await r.json(); this.showToast(e.error||'Ошибка',false); return; }
            this.showToast('Счётчик удалён');
            await this.loadSettings();
            await this.loadStatus();
          }catch(e){ this.showToast('Ошибка: '+e,false); }
        }
    };
  });
})();

// emulator.js — «Эмулятор счётчиков» (партия находок 0.23.1, П12; docs/design-emulator.md).
// Раздел «Настройки» набора v2, баннер «Обзора», метка «эмулятор» у приборов.
// Опрос — только через общий _pollTick (core.js): сводка баннера на «Обзоре» и полное
// состояние раздела на «Настройках». Своего таймера здесь нет.
(function () {
  window.WBEM = window.WBEM || { parts: [], conflicts: [] };

  window.WBEM.parts.push(function emulatorPart() {
    const J = { 'Content-Type': 'application/json' };
    const EMU_PREFIX = 'emu-';

    // Пустая форма прибора: никогда не null (открытие — отдельным флагом emuFormOpen).
    const blankForm = () => ({
      n: null, name: '', role: 'load', profile: 'daily',
      kw1: '1', kw2: '1', kw3: '1', pct: '3', sources: [],
    });

    return {
      // полное состояние раздела (GET /api/v2/emulator); null — ещё не загружено
      emu: null,
      emuUnavailable: false,
      emuBusy: false,
      emuError: '',
      // лёгкая сводка баннера «Обзора» (GET /api/v2/emulator/summary)
      emuSummary: { enabled: false, device_count: 0, banner: '', history_state: null, history_message: '' },
      // форма добавления/правки прибора
      emuFormOpen: false,
      emuForm: blankForm(),
      emuFormError: '',
      // длительности по умолчанию для кнопок неисправностей
      emuLossMin: 15,
      emuSparseH: 2,

      // ---- признаки -------------------------------------------------------
      // Метка «эмулятор» там, где сервер не присылает is_emulator (например,
      // строка с device_id в карточке узла): все приборы эмулятора начинаются с emu-.
      emuIs(deviceId) {
        return typeof deviceId === 'string' && deviceId.indexOf(EMU_PREFIX) === 0;
      },

      // Подпись прибора для выпадающих списков: «эмулятор» в названии уже есть
      // (сервер его требует), но переименованный в реестре прибор мог его потерять.
      _emuLabel(name, isEmu) {
        if (!isEmu) return name;
        return String(name).toLowerCase().indexOf('эмулятор') === -1 ? name + ' · эмулятор' : name;
      },

      emuOn() { return !!(this.emu && this.emu.enabled); },

      emuDevices() { return (this.emu && this.emu.devices) || []; },

      emuLoads() { return this.emuDevices().filter(d => d.role === 'load'); },

      emuBoundPoints() { return (this.emu && this.emu.bound_points) || []; },

      emuJournal() { return (this.emu && this.emu.journal) || []; },

      emuHistory() { return (this.emu && this.emu.history) || { state: null, message: '' }; },

      // Роли и профили — словари, как NODE_KINDS: списки в разметке строятся из них.
      emuRoles() {
        return [{ value: 'load', label: 'нагрузка' },
                { value: 'input', label: 'ввод (сумма нагрузок + %)' }];
      },

      emuProfiles() {
        return [{ value: 'daily', label: 'суточный (днём больше, ночью меньше)' },
                { value: 'flat', label: 'ровный' }];
      },

      emuRoleLabel(role) { return role === 'input' ? 'ввод' : 'нагрузка'; },

      emuProfileLabel(p) { return p === 'flat' ? 'ровная' : 'суточная'; },

      emuFaultLabel(d) {
        if (d.fault === 'loss') return 'нет связи';
        if (d.fault === 'sparse') return 'редкие показания';
        return 'работает';
      },

      // «до ЧЧ:ММ» для неисправности с длительностью
      emuFaultUntil(d) {
        const ts = d.fault === 'loss' ? d.loss_until : (d.fault === 'sparse' ? d.sparse_until : null);
        if (!ts) return '';
        return 'до ' + new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
      },

      emuSourcesText(d) {
        if (d.role !== 'input') return '';
        const by = {};
        this.emuDevices().forEach(x => { by[x.n] = x; });
        const names = (d.sources || []).map(n => (by[n] ? by[n].name : '№' + n));
        const pct = (d.pct > 0 ? '+' : '') + d.pct + ' %';
        return 'сумма: ' + (names.join(', ') || '—') + '; ' + pct;
      },

      emuTime(ts) {
        if (!ts) return '';
        return new Date(ts * 1000).toLocaleString('ru-RU', {
          day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
      },

      // Предупреждение о пустой истории показывается и когда проба не удалась по
      // другой причине (ошибка ≠ «истории нет»): текст приходит от сервера.
      emuHistoryWarning() {
        const h = this.emuHistory();
        return (h.state === 'missing' || h.state === 'error') ? (h.message || '') : '';
      },

      emuHistoryStateText() {
        const h = this.emuHistory();
        if (!this.emuOn()) return 'эмулятор выключен — проба не идёт';
        if (h.state === 'ok') return 'история эмулятора в wb-mqtt-db есть';
        if (h.state === 'missing') return 'истории эмулятора в wb-mqtt-db нет';
        if (h.state === 'error') return 'проверить историю не удалось';
        return 'проба истории ещё не проводилась (через 10–15 минут после включения)';
      },

      // ---- загрузка -------------------------------------------------------
      async loadEmulator(poll) {
        const t = this.feedStart('emu', { live: true, skipIfBusy: poll === true });
        if (!t) return;
        try {
          const r = await fetch('/api/v2/emulator', { signal: t.signal });
          const d = await r.json();
          if (!this.feedLive(t)) return;
          if (!r.ok) { this.feedFail(t, this._apiErrorMessage(d, r.status)); return; }
          // available=false — служба запущена без эмулятора (тестовый стенд)
          this.emuUnavailable = d.available === false;
          this.emu = d;
          this.emuSummary = this._emuSummaryFrom(d);
          this.feedDone(t, 'ready');
        } catch (e) {
          if (this.feedAborted(e)) return;
          this.feedFail(t, 'нет ответа');
        }
      },

      _emuSummaryFrom(d) {
        return {
          enabled: !!d.enabled, device_count: d.device_count || 0, banner: d.banner || '',
          history_state: (d.history || {}).state || null, history_message: (d.history || {}).message || '',
        };
      },

      async loadEmulatorSummary() {
        const t = this.feedStart('emu-sum', { live: true, skipIfBusy: true });
        if (!t) return;
        try {
          const r = await fetch('/api/v2/emulator/summary', { signal: t.signal });
          const d = await r.json();
          if (!this.feedLive(t)) return;
          if (!r.ok) { this.feedFail(t, this._apiErrorMessage(d, r.status)); return; }
          this.emuSummary = d;
          this.feedDone(t, 'ready');
        } catch (e) {
          if (this.feedAborted(e)) return;
          this.feedFail(t, 'нет ответа');
        }
      },

      // Списки приборов для «+ Счётчик» и т. п. грузятся один раз за сессию:
      // после включения/выключения и правки приборов эмулятора их надо перечитать.
      // Приборы появляются в реестре через несколько секунд (MQTT), поэтому
      // сброс повторяется один раз с задержкой. Не интервал, а разовый setTimeout.
      _emuInvalidateDevices() {
        this.structKnownDevicesLoaded = false;
        clearTimeout(this._emuInvTimer);
        this._emuInvTimer = setTimeout(() => { this.structKnownDevicesLoaded = false; }, 7000);
      },

      // ---- общий вызов API ------------------------------------------------
      async _emuCall(method, url, body) {
        this.emuBusy = true; this.emuError = '';
        try {
          const r = await fetch(url, {
            method, headers: J, body: body === undefined ? undefined : JSON.stringify(body) });
          let d = {};
          try { d = await r.json(); } catch (e) { /* пустое тело */ }
          if (!r.ok) {
            this.emuError = this._apiErrorMessage(d, r.status);
            return { ok: false, data: d };
          }
          return { ok: true, data: d };
        } catch (e) {
          this.emuError = 'Ошибка сети: ' + e;
          return { ok: false, data: {} };
        } finally {
          this.emuBusy = false;
        }
      },

      // ---- включение -------------------------------------------------------
      async emuSetEnabled(on) {
        const res = await this._emuCall('POST', '/api/v2/emulator/enabled', { enabled: !!on });
        if (res.ok) {
          this.emu = Object.assign({}, this.emu || {}, res.data);
          this.emuSummary = this._emuSummaryFrom(res.data);
          this.showToast(on ? 'Эмулятор включён: цифры с ним не настоящие'
                            : 'Эмулятор выключен, его топики стёрты', true);
          this._emuInvalidateDevices();
        }
        await this.loadEmulator();
      },

      async emuAddPreset() {
        const res = await this._emuCall('POST', '/api/v2/emulator/devices/preset', {});
        if (res.ok) {
          this.showToast('Добавлен типовой набор: три нагрузки и ввод над ними', true);
          this._emuInvalidateDevices();
        }
        await this.loadEmulator();
      },

      // ---- форма прибора ---------------------------------------------------
      emuOpenForm(dev) {
        this.emuFormError = '';
        if (dev) {
          const kw = dev.kw || [0, 0, 0];
          this.emuForm = {
            n: dev.n, name: dev.name, role: dev.role, profile: dev.profile,
            kw1: String(kw[0]), kw2: String(kw[1]), kw3: String(kw[2]),
            pct: String(dev.pct || 0), sources: (dev.sources || []).slice(),
          };
        } else {
          this.emuForm = blankForm();
        }
        this.emuFormOpen = true;
      },

      emuCloseForm() { this.emuFormOpen = false; this.emuFormError = ''; },

      emuToggleSource(n) {
        const s = this.emuForm.sources.slice();
        const i = s.indexOf(n);
        if (i === -1) s.push(n); else s.splice(i, 1);
        this.emuForm.sources = s;
      },

      emuFormValid() {
        const f = this.emuForm;
        if (f.role === 'input') return f.sources.length > 0 && f.pct !== '' && !isNaN(Number(f.pct));
        return [f.kw1, f.kw2, f.kw3].every(x => x !== '' && !isNaN(Number(x)) && Number(x) >= 0);
      },

      async emuSaveForm() {
        const f = this.emuForm;
        this.emuFormError = '';
        const body = { role: f.role, profile: f.profile };
        if (f.name.trim()) body.name = f.name.trim();
        if (f.role === 'input') {
          body.sources = f.sources.slice(); body.pct = Number(f.pct);
        } else {
          body.kw = [Number(f.kw1), Number(f.kw2), Number(f.kw3)];
        }
        const edit = f.n !== null;
        const res = await this._emuCall(
          edit ? 'PATCH' : 'POST',
          edit ? '/api/v2/emulator/devices/' + f.n : '/api/v2/emulator/devices', body);
        if (!res.ok) { this.emuFormError = this.emuError; this.emuError = ''; return; }
        this.emuFormOpen = false;
        this.showToast(edit ? 'Прибор эмулятора изменён' : 'Прибор эмулятора добавлен', true);
        this._emuInvalidateDevices();
        await this.loadEmulator();
      },

      emuAskRemove(dev) {
        this.askConfirm({
          kind: 'emu_remove_device', payload: { n: dev.n, name: dev.name },
          title: 'Удалить прибор эмулятора?',
          text: '«' + dev.name + '»: прибор перестанет публиковаться, его топики в брокере будут стёрты. '
                + 'Точка учёта, если она привязана, останется — её архивирует отдельная кнопка.',
          okLabel: 'Удалить прибор',
        });
      },

      // ---- неисправности ---------------------------------------------------
      // kind: reset | replace | loss | sparse. Длительность — из полей над таблицей.
      async emuFault(dev, kind) {
        const body = { kind };
        if (kind === 'loss') body.minutes = Number(this.emuLossMin);
        if (kind === 'sparse') body.hours = Number(this.emuSparseH);
        const res = await this._emuCall('POST', '/api/v2/emulator/devices/' + dev.n + '/fault', body);
        if (res.ok) {
          const text = (res.data.journal || [])[0];
          this.showToast(text ? text.text : 'Неисправность применена', true);
        }
        await this.loadEmulator();
      },

      async emuProbeHistory() {
        const res = await this._emuCall('POST', '/api/v2/emulator/probe-history', {});
        if (res.ok) this.showToast('Проба истории выполнена', true);
        await this.loadEmulator();
      },

      // ---- архивация точек эмулятора --------------------------------------
      emuAskArchive() {
        const pts = this.emuBoundPoints();
        this.askConfirm({
          kind: 'emu_archive_points', payload: {},
          title: 'Архивировать точки эмулятора?',
          text: 'Точки учёта, привязанные к приборам эмулятора, уйдут в архив. История сохранится, '
                + 'линии схемы освободятся.',
          lines: pts.map(p => p.name),
          okLabel: 'Архивировать точки (' + pts.length + ')',
        });
      },

      // Вызывается из scheme.js::_runConfirmAction для действий emu_*.
      async _emuRunConfirm(kind, p) {
        if (kind === 'emu_remove_device') {
          const r = await fetch('/api/v2/emulator/devices/' + p.n, { method: 'DELETE' });
          if (!r.ok) {
            let d = {}; try { d = await r.json(); } catch (e) { /* пусто */ }
            return this._confirmFail(this._apiErrorMessage(d, r.status));
          }
          this.showToast('Прибор эмулятора удалён', true);
          this._emuInvalidateDevices();
          await this.loadEmulator();
          return true;
        }
        if (kind === 'emu_archive_points') {
          const rev = await this._structCurrentRev();
          const r = await fetch('/api/v2/emulator/archive-points', {
            method: 'POST', headers: J,
            body: JSON.stringify({ confirm: true, expected_revision: rev }) });
          const d = await r.json();
          if (!r.ok) return this._confirmFail(this._apiErrorMessage(d, r.status));
          this.showToast(d.archived ? 'Точки эмулятора архивированы: ' + d.archived
                                    : 'Точек эмулятора для архивации нет', true);
          this._emuInvalidateDevices();
          await this.loadEmulator();
          return true;
        }
        return this._confirmFail('неизвестное действие: ' + kind);
      },
    };
  });
})();

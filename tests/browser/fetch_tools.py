"""Обёртка над fetch для браузерных сценариев (партия 13, 13.4–13.5).

Ставится ДО скриптов приложения (`open_browser(..., init_script=FETCH_WRAP)`), поэтому
приложение вызывает уже обёрнутый `fetch`. Что умеет (всё — через `window.*` страницы):

  window.__fail        список подстрок адреса: такие запросы «не отвечают»
                       (TypeError: Failed to fetch — как обрыв сети, без записи
                       «Failed to load resource» в консоль);
  window.__delay       {окончание_адреса: мс} — ответ позже на N мс (постоянно);
  window.__delayOnce   то же, но только для первого подходящего запроса;
  window.__fetchLog    журнал начатых запросов [{url, t}];
  window.__inflight    сколько запросов по адресу (с query) сейчас не завершено;
  window.__maxInflight наибольшее число одновременно не завершённых по адресу;
  window.__intervals   журнал setInterval [{id, ms, cleared}] — для проверки «один общий таймер».

Задержка уважает AbortSignal приложения: отменённый запрос завершается AbortError.
"""

FETCH_WRAP = """
(function(){
  const of = window.fetch.bind(window);
  window.__fail = []; window.__delay = {}; window.__delayOnce = {};
  window.__fetchLog = []; window.__inflight = {}; window.__maxInflight = {};
  window.fetch = function(u, o){
    const url = String((u && u.url) || u);
    window.__fetchLog.push({url: url, t: Date.now()});
    if (window.__fail.some(p => url.includes(p))) return Promise.reject(new TypeError('Failed to fetch'));
    const n = (window.__inflight[url] || 0) + 1;
    window.__inflight[url] = n;
    window.__maxInflight[url] = Math.max(window.__maxInflight[url] || 0, n);
    const done = () => { window.__inflight[url] = Math.max(0, (window.__inflight[url] || 1) - 1); };
    let ms = null;
    const once = Object.keys(window.__delayOnce).find(p => url.endsWith(p));
    if (once !== undefined) { ms = window.__delayOnce[once]; delete window.__delayOnce[once]; }
    else { const d = Object.keys(window.__delay).find(p => url.endsWith(p)); if (d !== undefined) ms = window.__delay[d]; }
    let p;
    if (ms === null) p = of(u, o);
    else {
      const sig = o && o.signal;
      p = new Promise((res, rej) => {
        const tm = setTimeout(() => of(u, o).then(res, rej), ms);
        if (sig) sig.addEventListener('abort', () => { clearTimeout(tm); rej(new DOMException('Aborted', 'AbortError')); });
      });
    }
    return p.then(r => { done(); return r; }, e => { done(); throw e; });
  };
  window.__intervals = [];
  const oi = window.setInterval.bind(window), oc = window.clearInterval.bind(window);
  window.setInterval = function(fn, ms){
    const id = oi.apply(null, arguments);
    window.__intervals.push({id: id, ms: ms, cleared: false});
    return id;
  };
  window.clearInterval = function(id){
    window.__intervals.forEach(i => { if (i.id === id) i.cleared = true; });
    return oc(id);
  };
})();
"""

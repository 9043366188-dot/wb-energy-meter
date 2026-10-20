"""Ожидания готовности для браузерных тестов (партия 13, 13.8, плавающие b02/b03).

Фиксированная пауза `wait_for_timeout(N)` на медленном процессоре (CI, нагрузка) бывает короче
реального действия: клик по карте приходит раньше, чем карта вписана и подогнана под контейнер,
а закрытие карточки — раньше, чем она открылась (и тогда карточка открывается ПОСЛЕ закрытия).
Здесь — ожидания по состоянию страницы.
"""
from __future__ import annotations

APP = "document.querySelector('[x-data]')._x_dataStack[0]"

# Карта «Плана v3» готова к клику: контейнер виден, размер карты равен размеру контейнера
# (invalidateSize отработал), отложенное вписывание выполнено, зум и сдвиг не анимируются.
MAP_V3_READY = """() => {
  if (typeof _planV3Map === 'undefined' || !_planV3Map) return false;
  const c = _planV3Map.getContainer();
  if (!c.offsetWidth || !c.offsetHeight) return false;
  const s = _planV3Map.getSize();
  if (s.x !== c.clientWidth || s.y !== c.clientHeight) return false;
  if (_planV3NeedsFit !== null) return false;
  if (_planV3Map._animatingZoom) return false;
  if (_planV3Map._panAnim && _planV3Map._panAnim._inProgress) return false;
  return true;
}"""

CARD_OPEN = "() => { const a = %s; return !!a.structInspector && !a.structInspectorLoading; }" % APP
CARD_CLOSED = "() => { const a = %s; return !a.structInspector && !a.structInspectorLoading; }" % APP


def wait_map_v3_ready(page, timeout: int = 15000) -> None:
    page.wait_for_function(MAP_V3_READY, timeout=timeout)


def wait_card_open(page, timeout: int = 15000) -> None:
    page.wait_for_function(CARD_OPEN, timeout=timeout)


def wait_card_closed(page, timeout: int = 15000) -> None:
    page.wait_for_function(CARD_CLOSED, timeout=timeout)


def close_card(page) -> None:
    """Дождаться, пока карточка откроется (иначе она откроется ПОСЛЕ закрытия), и закрыть её."""
    wait_card_open(page)
    page.evaluate(APP + ".closeInspector()")
    wait_card_closed(page)

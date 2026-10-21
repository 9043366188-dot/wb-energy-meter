"""Загрузка линии (партия 14, A31/A32, A43): единственная формула на все экраны.

load_percent = максимум фазного тока / rated_current_a × 100.

Считается только по току. Мощность вместо тока не подставляется (A32): нет
тока — нет процента, причина возвращается в load_unavailable_reason.

Какие фазы нужны (required_phases):
  * профиль phase_lN — только своя фаза N: остальные фазы прибора принадлежат
    другим точкам, их ток этой линии не относится (П9);
  * total_3p / нет профиля — все три фазы. Если нужной фазы нет в снимке,
    общий процент неизвестен: максимум по оставшимся занизил бы загрузку.

Причины (load_unavailable_reason), в порядке проверки:
  * no_rating     — у линии не указан номинальный ток (или он не положителен);
  * no_current    — нет точки учёта/привязки, прибор не отвечает, тока в
                    снимке нет совсем;
  * phase_missing — ток есть, но не по всем нужным фазам.
Ток для показа (current_a) не зависит от номинала: «нет номинала» не должно
прятать измеренный ток. При phase_missing current_a — максимум по имеющимся
фазам (как показывала карточка раньше), а load_percent = null и
missing_phases перечисляет недостающие.

Модуль чистый: без БД и Flask, чтобы тест A31/A32 и маршрут использовали одну
реализацию.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .binding_service import PROFILE_PHASE
from .model import PHASES

REASON_NO_RATING = "no_rating"
REASON_NO_CURRENT = "no_current"
REASON_PHASE_MISSING = "phase_missing"

# Состояния прибора, при которых ток из снимка — не «сейчас» (прибора нет на
# связи или он ни разу не появлялся): процент по такому току не показываем.
_NO_FRESH_STATUSES = ("no_connection", "never_seen")

REASON_TEXT = {
    REASON_NO_RATING: "не указан номинальный ток линии",
    REASON_NO_CURRENT: "нет измерения тока",
    REASON_PHASE_MISSING: "нет тока по одной из нужных фаз",
}


def required_phases(channel_profile: Optional[str]):
    """Фазы, без которых общий процент линии не считается."""
    phase = PROFILE_PHASE.get(channel_profile)
    return (phase,) if phase else tuple(PHASES)


def _result(rated, snap, *, current_a, missing, percent, reason):
    return {
        "rated_current_a": rated,
        "current_a": current_a,
        "phase_currents": (snap or {}).get("current_a"),
        "power_w": (snap or {}).get("power_w"),
        "missing_phases": list(missing),
        "load_percent": percent,
        "load_unavailable_reason": reason,
        "load_unavailable_text": REASON_TEXT.get(reason),
    }


def compute_line_load(rated_current_a: Optional[float],
                      snapshot: Optional[dict]) -> Dict[str, Any]:
    """snapshot — элемент ответа /api/v2/snapshot по основной точке линии
    (None, если у линии нет точки учёта)."""
    rated = rated_current_a
    has_rating = rated is not None and rated > 0
    snap = snapshot
    if snap is None or snap.get("binding_status") != "bound" \
            or snap.get("device_status") in _NO_FRESH_STATUSES:
        return _result(rated, snap, current_a=None, missing=(), percent=None,
                       reason=REASON_NO_RATING if not has_rating else REASON_NO_CURRENT)
    cur = snap.get("current_a")
    if not cur or all(cur.get(ph) is None for ph in PHASES):
        return _result(rated, snap, current_a=None, missing=(), percent=None,
                       reason=REASON_NO_RATING if not has_rating else REASON_NO_CURRENT)
    need = required_phases(snap.get("channel_profile"))
    present = [float(cur[ph]) for ph in need if cur.get(ph) is not None]
    missing = [ph for ph in need if cur.get(ph) is None]
    if not present:
        return _result(rated, snap, current_a=None, missing=missing, percent=None,
                       reason=REASON_NO_RATING if not has_rating else REASON_NO_CURRENT)
    peak = max(present)
    if not has_rating:
        return _result(rated, snap, current_a=peak, missing=missing, percent=None,
                       reason=REASON_NO_RATING)
    if missing:
        return _result(rated, snap, current_a=peak, missing=missing, percent=None,
                       reason=REASON_PHASE_MISSING)
    return _result(rated, snap, current_a=peak, missing=(),
                   percent=round(peak / float(rated) * 100.0, 1), reason=None)

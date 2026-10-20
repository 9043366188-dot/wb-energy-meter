"""Демо-данные для браузерных сценариев и снимков экранов (партия 13).

Один набор данных на все экраны, чтобы «до»- и «после»-скриншоты, а также
test_b14…b17 смотрели на одну и ту же схему:

    Ввод ──(счётчик «Ввод»)──▶ ЩР-1 ──▶ Освещение (счётчик)
                                  │──▶ Розетки   (счётчик)
                                  └──▶ Станок    (без счётчика)
    Ввод ──────────────────▶ ЩР-2 (без счётчика, без нагрузок)

Точки: «Ввод», «Освещение», «Розетки» с приборами и живой мощностью
(`Total P`, `Total AP energy`), почасовая энергия с начала текущего месяца,
учётная группа «Цех» из двух точек, один незанятый прибор (виден в MQTT, но
не привязан) — он попадает в «Требует внимания» и «Не на схеме». План
«Схема цеха» с элементами и линиями.

Использование:

    from demo_seed import seed_demo
    with Harness() as h:
        info = seed_demo(h)
"""
from __future__ import annotations

import time

from wb_energy_meter.group_repo_v2 import GroupRepoV2
from wb_energy_meter.model import ControlState, MeterStatus

HOUR = 3600

# device_id → (имя, мощность Вт, накопленная энергия кВт·ч, средний расход кВт·ч/час)
_METERS = {
    "dev-in": ("Ввод", 4800.0, 15210.4, 6.2),
    "dev-light": ("Освещение", 760.0, 3210.9, 0.9),
    "dev-sock": ("Розетки", 2150.0, 5120.3, 2.4),
}


def _live(h, device_id, name, power_w, energy_kwh, now):
    """Прибор виден в MQTT с живыми значениями (как после свежего сообщения)."""
    m = h.see_in_mqtt(device_id, name)
    m.controls["Total P"] = ControlState(name="Total P", value=power_w, raw_value=str(power_w),
                                          last_update_ts=now, meta={"type": "power"})
    m.controls["Total AP energy"] = ControlState(
        name="Total AP energy", value=energy_kwh, raw_value=str(energy_kwh),
        last_update_ts=now, meta={"type": "value"})
    m.controls["Serial"] = ControlState(name="Serial", value="SN-" + device_id.upper(),
                                        last_update_ts=now)
    m.last_any_ts = now
    m.last_measurement_ts = now
    m.status = MeterStatus.OK
    return m


def month_start_ts(now: float) -> int:
    """Начало текущего календарного месяца по UTC (достаточно для демо)."""
    t = time.gmtime(now)
    return int(time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, 0))) - time.timezone


def seed_demo(h, now: float | None = None, with_plan: bool = True) -> dict:
    """Заполнить стенд демо-схемой. Возвращает словарь id для сценариев."""
    now = now or time.time()
    out: dict = {"now": now}

    # --- приборы в MQTT -------------------------------------------------
    for dev, (name, p, e, _avg) in _METERS.items():
        _live(h, dev, name, p, e, now)
    # прибор, который виден, но никуда не привязан
    h.see_in_mqtt("dev-free", "Свободный прибор")

    # --- схема ----------------------------------------------------------
    vvod = h.make_node("vvod", "Ввод", "source")
    shr1 = h.make_node("shr1", "ЩР-1", "panel")
    shr2 = h.make_node("shr2", "ЩР-2", "panel")
    e_in = h.connect(vvod["id"], shr1["id"])
    e_shr2 = h.connect(vvod["id"], shr2["id"])
    c_light = h.add_consumer(shr1["id"], "Освещение")
    c_sock = h.add_consumer(shr1["id"], "Розетки")
    c_stan = h.add_consumer(shr1["id"], "Станок")
    out.update(vvod=vvod, shr1=shr1, shr2=shr2, e_in=e_in, e_shr2=e_shr2,
               light=c_light["node"], sock=c_sock["node"], stan=c_stan["node"],
               e_light=c_light["edge"], e_sock=c_sock["edge"], e_stan=c_stan["edge"])

    # --- точки учёта ----------------------------------------------------
    points = {}
    for dev, (name, _p, _e, _avg), edge in (
            ("dev-in", _METERS["dev-in"], e_in),
            ("dev-light", _METERS["dev-light"], c_light["edge"]),
            ("dev-sock", _METERS["dev-sock"], c_sock["edge"])):
        pt = h.make_point("pt-" + dev[4:], name)
        h.bind_meter(pt["id"], dev, name)
        h.set_meter_on_edge(edge["id"], pt["id"])
        points[dev] = pt
    out["points"] = points

    # --- энергия по часам с начала месяца ------------------------------
    start = month_start_ts(now)
    last_full = int(now // HOUR) * HOUR
    for dev, (_n, _p, _e, avg) in _METERS.items():
        t = start
        k = 0
        while t < last_full:
            # ровная «дневная» волна: днём больше, ночью меньше
            hour = (t // HOUR) % 24
            kwh = round(avg * (0.55 + 0.9 * (1 if 8 <= hour < 20 else 0.2)), 3)
            h.seed_energy(dev, kwh, period_start=t, period_end=t + HOUR)
            t += HOUR
            k += 1
    out["seeded_from"] = start

    # --- учётная группа -------------------------------------------------
    grp = GroupRepoV2(h.db).add("Цех")
    repo = GroupRepoV2(h.db)
    repo.add_member(grp.id, points["dev-light"]["id"], valid_from=0)
    repo.add_member(grp.id, points["dev-sock"]["id"], valid_from=0)
    out["group"] = grp

    # --- план ------------------------------------------------------------
    if with_plan:
        plan = h.create_plan("Схема цеха")
        out["plan"] = plan
        layout = {"Ввод": (240, 200), "ЩР-1": (620, 200), "ЩР-2": (620, 520),
                  "Освещение": (1000, 100), "Розетки": (1000, 300), "Станок": (1000, 500)}
        nodes = {n["name"]: n for n in (vvod, shr1, shr2, c_light["node"], c_sock["node"], c_stan["node"])}
        items = {}
        for name, (x, y) in layout.items():
            r = h.client.post(f"/api/v2/plans/{plan['id']}/items", json={
                "kind": "node", "node_id": nodes[name]["id"], "geometry": {"x": x, "y": y},
                "coord_space": "canvas_xy_v2", "label": name})
            assert r.status_code == 201, r.get_json()
            items[name] = r.get_json()
        out["plan_items"] = items
        for edge, a, b in ((e_in, "Ввод", "ЩР-1"), (e_shr2, "Ввод", "ЩР-2"),
                           (c_light["edge"], "ЩР-1", "Освещение"),
                           (c_sock["edge"], "ЩР-1", "Розетки"),
                           (c_stan["edge"], "ЩР-1", "Станок")):
            r = h.client.post(f"/api/v2/plans/{plan['id']}/edges", json={
                "edge_id": edge["id"], "from_item_id": items[a]["id"],
                "to_item_id": items[b]["id"], "view_kind": "structural"})
            assert r.status_code == 201, r.get_json()
    return out

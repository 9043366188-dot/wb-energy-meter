"""CLI-утилита wb-energy-meter-cli."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from typing import Optional

from . import __version__
from .config import DEFAULT_CONFIG_PATH, load_config
from .consumption import ConsumptionService
from .db import DEFAULT_DB_PATH, Database
from .periods import PERIOD_PRESETS, build_period, parse_user_datetime
from .repo import GroupRepo, KvRepo, MeterRepo
from .wb_db_client import HistoryChannel, RpcError, WbDbClient


log = logging.getLogger(__name__)


def _print_table(rows, headers):
    if not rows:
        print("(пусто)"); return
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(c)) for c in col) for col in cols]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    fmt = "| " + " | ".join("{:<" + str(w) + "}" for w in widths) + " |"
    print(sep); print(fmt.format(*headers)); print(sep)
    for r in rows:
        print(fmt.format(*[str(c) if c is not None else "" for c in r]))
    print(sep)


def _fmt_ts(ts):
    if not ts: return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))


# ---------------- meter commands ----------------

def cmd_meter_list(args, db, meters, groups, kv):
    items = meters.list_all(only_enabled=args.only_enabled)
    rows = []
    for m in items:
        rows.append([m.id, m.device_id, m.display_name,
                     m.group_name or "", m.serial_number or "",
                     m.role, "yes" if m.enabled else "no",
                     _fmt_ts(m.updated_at)])
    _print_table(rows, headers=["id", "device_id", "name", "group",
                                 "serial", "role", "enabled", "updated"])
    print(f"Всего: {len(items)}")
    return 0


def cmd_meter_add(args, db, meters, groups, kv):
    try:
        m = meters.add(device_id=args.device_id, display_name=args.name,
                       group=args.group, role=args.role or "consumer",
                       notes=args.notes)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr); return 2
    print(f"Добавлено: id={m.id} {m.device_id} -> {m.display_name!r}")
    if m.group_name: print(f"           группа: {m.group_name}")
    return 0


def cmd_meter_rename(args, db, meters, groups, kv):
    try: m = meters.update(args.device_id, display_name=args.name)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr); return 2
    print(f"Переименовано: {m.device_id} -> {m.display_name!r}")
    return 0


def cmd_meter_group(args, db, meters, groups, kv):
    try: m = meters.update(args.device_id, group=args.group)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr); return 2
    if m.group_name:
        print(f"{m.device_id}: группа -> {m.group_name!r}")
    else:
        print(f"{m.device_id}: группа снята")
    return 0


def cmd_meter_role(args, db, meters, groups, kv):
    try: m = meters.update(args.device_id, role=args.role)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr); return 2
    print(f"{m.device_id}: role -> {m.role}")
    return 0


def cmd_meter_enable(args, db, meters, groups, kv):
    try: m = meters.update(args.device_id, enabled=True)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr); return 2
    print(f"{m.device_id}: enabled"); return 0


def cmd_meter_disable(args, db, meters, groups, kv):
    try: m = meters.update(args.device_id, enabled=False)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr); return 2
    print(f"{m.device_id}: disabled"); return 0


def cmd_meter_remove(args, db, meters, groups, kv):
    if not args.yes:
        ans = input(f"Удалить {args.device_id} из реестра? [y/N]: ")
        if ans.strip().lower() != "y":
            print("Отменено."); return 1
    try: ok = meters.remove(args.device_id)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr); return 2
    if ok: print(f"Удалён: {args.device_id}"); return 0
    print(f"Не найден: {args.device_id}"); return 1


def cmd_meter_show(args, db, meters, groups, kv):
    m = meters.get_by_device_id(args.device_id)
    if not m:
        print(f"Не найден: {args.device_id}", file=sys.stderr); return 1
    print(f"id            : {m.id}")
    print(f"device_id     : {m.device_id}")
    print(f"display_name  : {m.display_name}")
    print(f"group         : {m.group_name or '—'} (id={m.group_id})")
    print(f"serial_number : {m.serial_number or '—'}")
    print(f"role          : {m.role}")
    print(f"enabled       : {m.enabled}")
    print(f"notes         : {m.notes or '—'}")
    print(f"created_at    : {_fmt_ts(m.created_at)}")
    print(f"updated_at    : {_fmt_ts(m.updated_at)}")
    return 0


# ---------------- group commands ----------------

def cmd_group_list(args, db, meters, groups, kv):
    items = groups.list_all()
    rows = [[g.id, g.name, _fmt_ts(g.created_at)] for g in items]
    _print_table(rows, headers=["id", "name", "created"])
    print(f"Всего: {len(items)}")
    return 0


def cmd_group_remove(args, db, meters, groups, kv):
    g = groups.get_by_id(args.group_id)
    if not g:
        print(f"Не найдена: id={args.group_id}", file=sys.stderr); return 1
    if not args.yes:
        ans = input(f"Удалить группу {g.name!r}? Счётчики останутся, "
                    f"но без группы. [y/N]: ")
        if ans.strip().lower() != "y":
            print("Отменено."); return 1
    ok = groups.delete(args.group_id)
    if ok: print(f"Удалена: {g.name}"); return 0
    return 1


# ---------------- scan ----------------

def cmd_scan(args, db, meters, groups, kv):
    import paho.mqtt.client as mqtt
    import threading as _thr

    cfg = load_config(args.config)
    found = _collect_mqtt_devices(
        host=cfg.mqtt.host, port=cfg.mqtt.port,
        prefix=cfg.device_prefix, duration_s=args.duration)

    in_db = {m.device_id: m for m in meters.list_all()}
    new = sorted([d for d in found if d not in in_db])
    in_both = sorted([d for d in found if d in in_db])
    only_db = sorted([d for d in in_db if d not in found])

    print(f"\n--- Найдены в MQTT ({len(found)}) ---")
    for d in sorted(found):
        info = found[d]
        marker = "✓" if d in in_db else "+"
        print(f"  {marker} {d}  (имя: {info.get('name', '—')!r}, "
              f"serial: {info.get('serial', '—')!r})")
    if only_db:
        print(f"\n--- В реестре, но не в MQTT ({len(only_db)}) ---")
        for d in only_db:
            print(f"  ? {d}  (имя в БД: {in_db[d].display_name!r})")
    print(f"\nИтого: {len(in_both)} в реестре и MQTT, "
          f"{len(new)} новых, {len(only_db)} только в БД")

    if args.add_all and new:
        print(f"\n--- Добавляю в реестр ({len(new)}) ---")
        for did in new:
            info = found[did]
            name = info.get("name") or did
            try:
                m = meters.add(device_id=did, display_name=name)
                if info.get("serial"):
                    meters.update_serial_observed(did, info["serial"])
                print(f"  + {did} -> {m.display_name!r}")
            except ValueError as e:
                print(f"  ! {did}: {e}")
    return 0


def _collect_mqtt_devices(host, port, prefix, duration_s):
    import paho.mqtt.client as mqtt
    import threading as _thr
    devices = {}
    lock = _thr.Lock()

    def on_connect(c, u, f, rc):
        if rc == 0:
            c.subscribe("/devices/+/meta/name", qos=0)
            c.subscribe("/devices/+/meta", qos=0)
            c.subscribe("/devices/+/controls/Serial", qos=0)

    def on_message(c, u, msg):
        try:
            t = msg.topic
            payload = msg.payload.decode("utf-8", errors="replace")
            parts = t.split("/")
            if len(parts) < 4: return
            did = parts[2]
            if not did.startswith(prefix): return
            with lock:
                d = devices.setdefault(did, {})
                tail = "/".join(parts[3:])
                if tail == "meta/name":
                    d["name"] = payload.strip()
                elif tail == "meta":
                    try:
                        obj = json.loads(payload)
                        if isinstance(obj, dict):
                            t_field = obj.get("title") or obj.get("name")
                            if isinstance(t_field, dict):
                                t_field = t_field.get("ru") or t_field.get("en")
                            if isinstance(t_field, str) and not d.get("name"):
                                d["name"] = t_field.strip()
                    except json.JSONDecodeError: pass
                elif tail == "controls/Serial":
                    d["serial"] = payload.strip()
        except Exception: pass

    client = mqtt.Client(client_id=f"wb-em-cli-{uuid.uuid4().hex[:8]}",
                         clean_session=True)
    client.on_connect = on_connect
    client.on_message = on_message
    print(f"Подключаюсь к MQTT {host}:{port} ...", flush=True)
    try: client.connect(host, port, keepalive=10)
    except Exception as e:
        print(f"Ошибка подключения: {e}", file=sys.stderr); return {}
    client.loop_start()
    try:
        print(f"Слушаю {duration_s:.0f} с ...", flush=True)
        time.sleep(duration_s)
    finally:
        client.loop_stop(); client.disconnect()
    return devices


# ---------------- db / config ----------------

def cmd_db_status(args, db, meters, groups, kv):
    s = db.stats()
    print(f"path           : {s['path']}")
    print(f"size           : {s['size_bytes']:,} bytes "
          f"({s['size_bytes']/1024:.1f} KB)")
    print(f"schema_version : {s['schema_version']}")
    print(f"\nTable counts:")
    for t, n in s["table_counts"].items():
        print(f"  {t:25s} {n if n is not None else '—'}")
    return 0


def cmd_db_vacuum(args, db, meters, groups, kv):
    db.vacuum(); print("VACUUM выполнен"); return 0


def cmd_config_show(args, db, meters, groups, kv):
    cfg = load_config(args.config)
    out = {
        "mqtt": {"host": cfg.mqtt.host, "port": cfg.mqtt.port,
                 "username": cfg.mqtt.username, "keepalive": cfg.mqtt.keepalive},
        "http": {"host": cfg.http.host, "port": cfg.http.port},
        "device_prefix": cfg.device_prefix, "log_file": cfg.log_file,
        "status": cfg.status.__dict__,
        "meters_in_yaml": [
            {"device_id": m.device_id, "display_name": m.display_name,
             "group": m.group}
            for m in cfg.meters
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_config_validate(args, db, meters, groups, kv):
    try: cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr); return 2
    print(f"Конфиг {args.config} валиден.")
    print(f"  MQTT:    {cfg.mqtt.host}:{cfg.mqtt.port}")
    print(f"  HTTP:    {cfg.http.host}:{cfg.http.port}")
    print(f"  Реестр:  {len(cfg.meters)} счётчиков")
    return 0


# ---------------- Шаг 3: consumption / history ----------------

def _make_wb_db_client(args):
    cfg = load_config(args.config)
    return WbDbClient(
        broker=cfg.mqtt.host, port=cfg.mqtt.port,
        username=cfg.mqtt.username, password=cfg.mqtt.password,
        default_timeout_s=15.0)


def _build_period_from_args(args):
    if getattr(args, "from_date", None) and getattr(args, "to_date", None):
        return build_period(
            ts_from=parse_user_datetime(args.from_date),
            ts_to=parse_user_datetime(args.to_date))
    preset = getattr(args, "period", None) or "today"
    return build_period(preset)


def cmd_consumption(args, db, meters, groups, kv):
    m = meters.get_by_device_id(args.device_id)
    if not m:
        print(f"Счётчик не найден в реестре: {args.device_id}",
              file=sys.stderr); return 2
    try: period = _build_period_from_args(args)
    except ValueError as e:
        print(f"Ошибка периода: {e}", file=sys.stderr); return 2

    client = _make_wb_db_client(args)
    svc = ConsumptionService(client)
    try: result = svc.calculate(args.device_id, period)
    except RpcError as e:
        print(f"Ошибка wb-mqtt-db: {e}", file=sys.stderr); return 3

    if args.json:
        d = result.to_dict(); d["display_name"] = m.display_name
        print(json.dumps(d, ensure_ascii=False, indent=2)); return 0

    print(f"Счётчик   : {m.display_name}  ({args.device_id})")
    print(f"Период    : {period.description}")
    print(f"            с {_fmt_ts(int(period.ts_from))} "
          f"по {_fmt_ts(int(period.ts_to))}")
    if result.consumption_kwh is None:
        print(f"Расход    : —  ({result.quality})")
    else:
        print(f"Расход    : {result.consumption_kwh:.4f} кВт·ч")
        if (result.ap_energy_start is not None
                and result.ap_energy_end is not None):
            print(f"            ({result.ap_energy_start} -> "
                  f"{result.ap_energy_end} кВт·ч)")
    print(f"Качество  : {result.quality}")
    print(f"Точек в периоде: {result.samples_in_period}")
    if result.ts_start_actual:
        print(f"Граничные точки:")
        print(f"  start: {_fmt_ts(int(result.ts_start_actual))}  "
              f"value={result.ap_energy_start}")
    if result.ts_end_actual:
        print(f"  end:   {_fmt_ts(int(result.ts_end_actual))}  "
              f"value={result.ap_energy_end}")
    if result.warnings:
        print("Предупреждения:")
        for w in result.warnings:
            print(f"  - {w}")
    return 0


def cmd_consumption_summary(args, db, meters, groups, kv):
    items = meters.list_all(only_enabled=True)
    if not items:
        print("В реестре нет счётчиков."); return 0
    try: period = _build_period_from_args(args)
    except ValueError as e:
        print(f"Ошибка периода: {e}", file=sys.stderr); return 2

    client = _make_wb_db_client(args)
    svc = ConsumptionService(client)

    print(f"Период: {period.description}")
    print(f"        с {_fmt_ts(int(period.ts_from))} "
          f"по {_fmt_ts(int(period.ts_to))}")
    print()

    rows = []; total = 0.0; has_unknown = False
    for m in items:
        try: r = svc.calculate(m.device_id, period)
        except RpcError as e:
            rows.append([m.id, m.device_id, m.display_name, "—",
                         f"err: {e}"])
            has_unknown = True; continue
        if r.consumption_kwh is None:
            rows.append([m.id, m.device_id, m.display_name, "—", r.quality])
            has_unknown = True
        else:
            rows.append([m.id, m.device_id, m.display_name,
                         f"{r.consumption_kwh:.4f}", r.quality])
            total += r.consumption_kwh

    _print_table(rows, headers=["id", "device_id", "name", "kWh", "quality"])
    print()
    print(f"Итого по {len(items)} счётчикам: {total:.4f} кВт·ч"
          + (" (некоторые показатели неизвестны)" if has_unknown else ""))
    return 0


def cmd_history_info(args, db, meters, groups, kv):
    client = _make_wb_db_client(args)
    try: channels = client.get_channels(timeout_s=10.0)
    except RpcError as e:
        print(f"Ошибка: {e}", file=sys.stderr); return 3

    target = getattr(args, "device_id", None)
    if target:
        channels = [c for c in channels if c.device == target]
        if not channels:
            print(f"В истории нет каналов устройства {target}")
            print("Возможные причины:")
            print("  - устройство не настроено в /etc/wb-mqtt-db.conf")
            print("  - другое имя в реестре")
            return 1

    channels.sort(key=lambda c: (c.device, c.control))
    rows = []
    for c in channels:
        last = _fmt_ts(c.last_ts) if c.last_ts else "—"
        rows.append([c.device, c.control, c.items, last])
    _print_table(rows, headers=["device", "control", "items", "last_ts"])
    print(f"Всего: {len(channels)} каналов")
    return 0


def cmd_history_show(args, db, meters, groups, kv):
    client = _make_wb_db_client(args)
    try: period = _build_period_from_args(args)
    except ValueError as e:
        print(f"Ошибка периода: {e}", file=sys.stderr); return 2

    try:
        points = client.get_values(
            device=args.device_id, control=args.channel,
            ts_from=period.ts_from, ts_to=period.ts_to,
            limit=int(args.limit), timeout_s=15.0)
    except RpcError as e:
        print(f"Ошибка wb-mqtt-db: {e}", file=sys.stderr); return 3

    print(f"Канал:  {args.device_id} / {args.channel}")
    print(f"Период: {period.description}")
    print(f"Точек:  {len(points)}")
    if not points: return 0

    print()
    rows = []
    show = points if args.all else points[:30]
    for p in show:
        rows.append([_fmt_ts(int(p.timestamp)), f"{p.value}"])
    _print_table(rows, headers=["timestamp", "value"])
    if not args.all and len(points) > 30:
        print(f"... показаны первые 30 из {len(points)} (см. --all)")
    return 0


# ---------------- parser ----------------

def build_parser():
    p = argparse.ArgumentParser(prog="wb-energy-meter-cli")
    p.add_argument("--db-path", default=DEFAULT_DB_PATH)
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    p.add_argument("--version", action="version",
                   version=f"wb-energy-meter-cli {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    # meter
    pm = sub.add_parser("meter", help="управление счётчиками")
    sm = pm.add_subparsers(dest="subcmd", required=True)
    s = sm.add_parser("list"); s.add_argument("--only-enabled", action="store_true")
    s.set_defaults(func=cmd_meter_list)
    s = sm.add_parser("add"); s.add_argument("device_id")
    s.add_argument("--name", required=True); s.add_argument("--group")
    s.add_argument("--role", choices=["input", "consumer", "other"])
    s.add_argument("--notes"); s.set_defaults(func=cmd_meter_add)
    s = sm.add_parser("rename"); s.add_argument("device_id")
    s.add_argument("--name", required=True); s.set_defaults(func=cmd_meter_rename)
    s = sm.add_parser("group"); s.add_argument("device_id")
    s.add_argument("--group", required=True); s.set_defaults(func=cmd_meter_group)
    s = sm.add_parser("role"); s.add_argument("device_id")
    s.add_argument("--role", required=True,
                   choices=["input", "consumer", "other"])
    s.set_defaults(func=cmd_meter_role)
    s = sm.add_parser("enable"); s.add_argument("device_id")
    s.set_defaults(func=cmd_meter_enable)
    s = sm.add_parser("disable"); s.add_argument("device_id")
    s.set_defaults(func=cmd_meter_disable)
    s = sm.add_parser("remove"); s.add_argument("device_id")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_meter_remove)
    s = sm.add_parser("show"); s.add_argument("device_id")
    s.set_defaults(func=cmd_meter_show)

    # group
    pg = sub.add_parser("group", help="группы")
    sg = pg.add_subparsers(dest="subcmd", required=True)
    s = sg.add_parser("list"); s.set_defaults(func=cmd_group_list)
    s = sg.add_parser("remove"); s.add_argument("group_id", type=int)
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_group_remove)

    # scan
    s = sub.add_parser("scan")
    s.add_argument("--duration", type=float, default=5.0)
    s.add_argument("--add-all", action="store_true")
    s.set_defaults(func=cmd_scan)

    # db
    pd = sub.add_parser("db"); sd = pd.add_subparsers(dest="subcmd", required=True)
    s = sd.add_parser("status"); s.set_defaults(func=cmd_db_status)
    s = sd.add_parser("vacuum"); s.set_defaults(func=cmd_db_vacuum)

    # config
    pc = sub.add_parser("config"); sc = pc.add_subparsers(dest="subcmd", required=True)
    s = sc.add_parser("show"); s.set_defaults(func=cmd_config_show)
    s = sc.add_parser("validate"); s.set_defaults(func=cmd_config_validate)

    # Step 3: consumption / history
    # consumption
    s = sub.add_parser("consumption",
                       help="расход энергии за период для одного счётчика")
    s.add_argument("device_id")
    s.add_argument("--period", choices=list(PERIOD_PRESETS),
                   help="предустановленный период")
    s.add_argument("--from", dest="from_date",
                   help="начало периода: YYYY-MM-DD или 'YYYY-MM-DD HH:MM'")
    s.add_argument("--to", dest="to_date", help="конец периода")
    s.add_argument("--json", action="store_true",
                   help="вывод в JSON")
    s.set_defaults(func=cmd_consumption)

    # consumption-summary
    s = sub.add_parser("consumption-summary",
                       help="расход по всем счётчикам за период")
    s.add_argument("--period", choices=list(PERIOD_PRESETS))
    s.add_argument("--from", dest="from_date")
    s.add_argument("--to", dest="to_date")
    s.set_defaults(func=cmd_consumption_summary)

    # history-info
    s = sub.add_parser("history-info",
                       help="какие каналы есть в wb-mqtt-db")
    s.add_argument("device_id", nargs="?", default=None,
                   help="device_id (необязательно — иначе все каналы)")
    s.set_defaults(func=cmd_history_info)

    # history-show
    s = sub.add_parser("history-show",
                       help="показать историю канала")
    s.add_argument("device_id")
    s.add_argument("channel", help="имя канала, например 'Total AP energy'")
    s.add_argument("--period", choices=list(PERIOD_PRESETS),
                   default="last_24h")
    s.add_argument("--from", dest="from_date")
    s.add_argument("--to", dest="to_date")
    s.add_argument("--limit", type=int, default=10000)
    s.add_argument("--all", action="store_true",
                   help="показать все точки, не только первые 30")
    s.set_defaults(func=cmd_history_show)

    return p


def main(argv=None):
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    db = Database(args.db_path)
    db.open()
    try:
        groups = GroupRepo(db); meters = MeterRepo(db, groups); kv = KvRepo(db)
        return args.func(args, db, meters, groups, kv)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr); return 2
    except KeyboardInterrupt:
        print(); return 130
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

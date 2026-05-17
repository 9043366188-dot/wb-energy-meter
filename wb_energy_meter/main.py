"""Точка входа демона."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from . import __version__
from .api import ApiServer
from .background import BackgroundTasks
from .config import DEFAULT_CONFIG_PATH, load_config
from .consumption import ConsumptionService
from .db import DEFAULT_DB_PATH, Database
from .logger import setup_logging
from .model import MeterRegistry
from .mqtt_client import MqttService
from .repo import GroupRepo, KvRepo, MeterRepo, import_registry_from_config
from .status import StatusEngine
from .wb_db_client import WbDbClient


log = logging.getLogger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wb-energy-meter")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--no-log-file", action="store_true")
    parser.add_argument("--version", action="version",
                        version=f"wb-energy-meter {__version__}")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except FileNotFoundError as e:
        print(f"[fatal] {e}", file=sys.stderr); return 2
    except ValueError as e:
        print(f"[fatal] Ошибка конфига: {e}", file=sys.stderr); return 2

    log_file = None if args.no_log_file else cfg.log_file
    setup_logging(args.log_level, log_file)

    log.info("=" * 60)
    log.info("wb-energy-meter %s — запуск", __version__)
    log.info("Конфиг:    %s", args.config)
    log.info("БД:        %s", args.db_path)
    log.info("MQTT:      %s:%d", cfg.mqtt.host, cfg.mqtt.port)
    log.info("HTTP API:  %s:%d", cfg.http.host, cfg.http.port)
    log.info("Префикс:   %s", cfg.device_prefix)

    db = Database(path=args.db_path)
    try: db.open()
    except Exception as e:
        log.error("Не удалось открыть БД: %s", e); return 4

    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)
    kv_repo = KvRepo(db)

    try:
        added = import_registry_from_config(meters_repo, kv_repo, cfg.meters)
        if added > 0:
            log.info("Из YAML в БД импортировано счётчиков: %d", added)
    except Exception as e:
        log.error("Ошибка импорта реестра: %s", e)

    registry = MeterRegistry()
    db_meters = meters_repo.list_all(only_enabled=True)
    log.info("Счётчиков в реестре (из БД): %d", len(db_meters))
    for m in db_meters:
        log.info("  - %s -> %r (группа: %s)",
                 m.device_id, m.display_name, m.group_name or "—")
    registry.apply_registry_config([
        {"device_id": m.device_id, "display_name": m.display_name,
         "group": m.group_name}
        for m in db_meters
    ])
    if len(db_meters) == 0:
        log.warning("В реестре БД нет счётчиков. Добавьте через CLI.")

    mqtt_service = MqttService(
        broker=cfg.mqtt.host, port=cfg.mqtt.port,
        registry=registry, device_prefix=cfg.device_prefix,
        keepalive=cfg.mqtt.keepalive,
        username=cfg.mqtt.username, password=cfg.mqtt.password,
        client_id_prefix=cfg.mqtt.client_id_prefix)
    connected = mqtt_service.start(connect_timeout=5.0)
    if not connected:
        log.warning("MQTT не подключился за 5 сек, продолжаем в фоне")

    status_engine = StatusEngine(registry, cfg.status, interval_s=2.0)
    status_engine.start()

    bg_tasks = BackgroundTasks(registry, meters_repo, interval_s=60.0)
    bg_tasks.start()

    wb_db_client = WbDbClient(
        broker=cfg.mqtt.host, port=cfg.mqtt.port,
        username=cfg.mqtt.username, password=cfg.mqtt.password,
        default_timeout_s=10.0)
    consumption_service = ConsumptionService(wb_db_client)
    log.info("Расчёт расхода через wb-mqtt-db RPC: готов")

    api = ApiServer(
        host=cfg.http.host, port=cfg.http.port,
        registry=registry, meters_repo=meters_repo,
        is_mqtt_connected=lambda: mqtt_service.is_connected,
        mqtt_message_count=lambda: mqtt_service.message_count,
        mqtt_error_count=lambda: mqtt_service.error_count,
        wb_db_client=wb_db_client,
        consumption_service=consumption_service)
    try:
        api.start()
    except OSError as e:
        log.error("Не удалось запустить HTTP API: %s", e)
        bg_tasks.stop(); status_engine.stop()
        mqtt_service.stop(); db.close(); return 3

    stop_event = threading.Event()
    def _shutdown(signum, frame):
        log.info("Сигнал %d, останавливаемся...", signum)
        stop_event.set()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Запуск завершён.")
    try:
        stop_event.wait()
    finally:
        log.info("Остановка...")
        for stopper, name in [
            (api.stop, "api"), (bg_tasks.stop, "bg_tasks"),
            (status_engine.stop, "status"), (mqtt_service.stop, "mqtt"),
            (db.close, "db"),
        ]:
            try: stopper()
            except Exception as e: log.debug("%s.stop: %s", name, e)
        log.info("Остановлен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

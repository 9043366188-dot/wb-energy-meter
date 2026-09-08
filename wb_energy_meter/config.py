"""Загрузка YAML-конфига."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = "/etc/wb-energy-meter.conf"


@dataclass
class MqttConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    keepalive: int = 30
    client_id_prefix: str = "wb-energy-meter"


@dataclass
class HttpConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class MeterEntry:
    device_id: str
    display_name: str
    group: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "MeterEntry":
        if "device_id" not in d:
            raise ValueError(f"meter entry missing 'device_id': {d}")
        if "display_name" not in d:
            raise ValueError(
                f"meter entry missing 'display_name' for device_id={d['device_id']}"
            )
        return cls(
            device_id=str(d["device_id"]).strip(),
            display_name=str(d["display_name"]).strip(),
            group=(str(d["group"]).strip() if d.get("group") else None),
        )


@dataclass
class StatusConfig:
    # Нет ЛЮБЫХ сообщений от устройства дольше этого времени → no_connection.
    # Для event-driven счётчиков (WB-MAP3E fw2) рекомендуется 600-1800 с,
    # потому что счётчик молчит когда нет нагрузки, и опрос серийника
    # раз в минуту — единственный признак жизни.
    no_connection_timeout_s: int = 600

    # Данные обновлялись, но давно — предупреждение.
    stale_warning_timeout_s: int = 120

    undervoltage_v: float = 198.0
    overvoltage_v: float = 253.0
    phase_lost_v: float = 150.0
    freq_min_hz: float = 49.0
    freq_max_hz: float = 51.0


@dataclass
class AggregatorConfigYaml:
    """Конфигурация воркера почасовых агрегатов (Шаг 4)."""
    enabled: bool = True
    catchup_days: int = 90
    max_catchup_duration_s: int = 300
    recompute_recent_h: int = 168
    hour_offset_s: int = 90
    catchup_start_delay_s: int = 10
    patcher_interval_h: int = 6
    rpc_timeout_s: float = 15.0
    rpc_max_retries: int = 3


@dataclass
class UpdateConfig:
    """Самообновление из GitHub по кнопке (ТЗ v0.9.0). Значения по
    умолчанию заданы здесь же, чтобы старые конфиги без секции
    `update:` продолжали работать без изменений."""
    enabled: bool = True
    # Кнопка обновления доступна всем в локальной сети (у сервиса нет
    # аутентификации, см. README "Обновление" и AGENTS.md) — если
    # контроллер в недоверенной сети, выставьте False или http.host:
    # 127.0.0.1.
    allow_from_ui: bool = True
    repo_owner: str = "9043366188-dot"
    repo_name: str = "wb-energy-meter"
    ref: str = "main"
    check_timeout_s: int = 10


@dataclass
class AppConfig:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    status: StatusConfig = field(default_factory=StatusConfig)
    aggregator: AggregatorConfigYaml = field(default_factory=AggregatorConfigYaml)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    meters: list[MeterEntry] = field(default_factory=list)
    device_prefix: str = "wb-map3e_"
    log_file: str | None = "/var/log/wb-energy-meter/wb-energy-meter.log"


def load_config(path: str) -> AppConfig:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ValueError(f"Config root must be a mapping")

    cfg = AppConfig()

    mqtt_raw = raw.get("mqtt") or {}
    cfg.mqtt = MqttConfig(
        host=str(mqtt_raw.get("host", cfg.mqtt.host)),
        port=int(mqtt_raw.get("port", cfg.mqtt.port)),
        username=mqtt_raw.get("username") or None,
        password=mqtt_raw.get("password") or None,
        keepalive=int(mqtt_raw.get("keepalive", cfg.mqtt.keepalive)),
        client_id_prefix=str(mqtt_raw.get("client_id_prefix",
                                          cfg.mqtt.client_id_prefix)),
    )

    http_raw = raw.get("http") or {}
    cfg.http = HttpConfig(
        host=str(http_raw.get("host", cfg.http.host)),
        port=int(http_raw.get("port", cfg.http.port)),
    )

    status_raw = raw.get("status") or {}
    sd = StatusConfig()
    for f_name in ("no_connection_timeout_s", "stale_warning_timeout_s",
                   "undervoltage_v", "overvoltage_v", "phase_lost_v",
                   "freq_min_hz", "freq_max_hz"):
        if f_name in status_raw:
            setattr(sd, f_name, type(getattr(sd, f_name))(status_raw[f_name]))
    cfg.status = sd

    aggr_raw = raw.get("aggregator") or {}
    ag = AggregatorConfigYaml()
    for f_name in ("enabled", "catchup_days", "max_catchup_duration_s",
                   "recompute_recent_h", "hour_offset_s",
                   "catchup_start_delay_s", "patcher_interval_h",
                   "rpc_timeout_s", "rpc_max_retries"):
        if f_name in aggr_raw:
            cur_type = type(getattr(ag, f_name))
            setattr(ag, f_name, cur_type(aggr_raw[f_name]))
    cfg.aggregator = ag

    update_raw = raw.get("update") or {}
    uc = UpdateConfig()
    for f_name in ("enabled", "allow_from_ui", "repo_owner", "repo_name",
                   "ref", "check_timeout_s"):
        if f_name in update_raw:
            cur_type = type(getattr(uc, f_name))
            setattr(uc, f_name, cur_type(update_raw[f_name]))
    cfg.update = uc

    meters_raw = raw.get("meters") or []
    cfg.meters = [MeterEntry.from_dict(m) for m in meters_raw]
    seen: set[str] = set()
    for m in cfg.meters:
        if m.device_id in seen:
            raise ValueError(f"Duplicate device_id in meters: {m.device_id}")
        seen.add(m.device_id)

    cfg.device_prefix = str(raw.get("device_prefix", cfg.device_prefix))
    cfg.log_file = raw.get("log_file", cfg.log_file)
    if cfg.log_file is not None:
        cfg.log_file = str(cfg.log_file)
    return cfg


def registry_to_dicts(cfg: AppConfig) -> list[dict[str, Any]]:
    return [
        {"device_id": m.device_id, "display_name": m.display_name,
         "group": m.group}
        for m in cfg.meters
    ]

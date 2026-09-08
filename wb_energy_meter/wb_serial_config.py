"""Чтение и (за флагом) точечная правка /etc/wb-mqtt-serial.conf — ТЗ v0.10.0.

Только стандартная библиотека.

Зачем это вообще нужно: WB-MAP3E fw2 работает event-driven и молчит,
пока не меняется нагрузка. Отличить «нет связи» от «нет нагрузки»
позволяет канал `Uptime` — он растёт каждые 2 секунды. В конфиге
драйвера это свойство канала `"enabled": true` («in queue order» в
Device Manager).

ГЛАВНОЕ ПРО ЭТОТ МОДУЛЬ. `/etc/wb-mqtt-serial.conf` — **чужой** файл.
В нём описаны ВСЕ устройства контроллера: реле, датчики, приводы. Ошибка
записи роняет весь ввод-вывод объекта, а не только учёт электроэнергии.
Поэтому:

- по умолчанию модуль работает **только на чтение** (диагностика);
- запись возможна лишь при `wb_serial.allow_edit: true` в конфиге
  сервиса и касается ровно одного ключа — `enabled` у канала `Uptime`
  одного конкретного устройства;
- файл, который не разбирается как чистый JSON (комментарии `//`, JSON5,
  битый), не правится **никогда** — только отказ с внятным текстом.
  «Умных» починок и вычисток комментариев здесь нет и быть не должно;
- перед записью — бэкап и сверка sha256 (гонка с веб-интерфейсом WB);
- запись атомарная (tmp + fsync + os.replace) с сохранением прав и
  владельца, после записи — обязательная перечитка с откатом при любой
  неожиданности.

RPC на изменение конфигурации у драйвера нет (проверено по README
wb-mqtt-serial), `/etc/wb-mqtt-serial.conf.d/` — только для шаблонов.
Поэтому путь ровно один: правка файла + `systemctl restart`.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

UPTIME_CHANNEL = "Uptime"

DEFAULT_CONFIG_PATH = "/etc/wb-mqtt-serial.conf"
DEFAULT_TEMPLATES_DIRS = (
    "/usr/share/wb-mqtt-serial/templates",
    "/etc/wb-mqtt-serial.conf.d/templates",
)
DEFAULT_BACKUP_DIR = "/mnt/data/var/lib/wb-energy-meter/wb-serial-backups"
DEFAULT_SERVICE_NAME = "wb-mqtt-serial"

KEEP_BACKUPS = 10

# Имя юнита systemd мы берём из конфига сервиса (не из HTTP-запроса), но
# на всякий случай не пускаем в командную строку ничего экзотического.
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9@._-]+$")


class WbSerialConfigError(Exception):
    """Не удалось прочитать/разобрать конфиг драйвера, либо запись
    отклонена. Текст — по-русски, отдаётся прямо в API-ответе."""


class WbSerialConflict(WbSerialConfigError):
    """Файл изменился между чтением и записью (гонка с веб-интерфейсом
    WB) — писать нельзя, нужно перечитать и повторить."""


# ---------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------

def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_config(path: str) -> Tuple[dict, str]:
    """Читает конфиг драйвера. Возвращает (data, sha256).

    Бросает WbSerialConfigError с человеческим текстом при отсутствии
    файла, отказе в правах и невалидном JSON. Никаких попыток «починить»
    файл: комментарии `//`, JSON5 и прочее — это отказ, а не задача.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise WbSerialConfigError(
            "Файл конфигурации драйвера не найден: %s" % path)
    except PermissionError:
        raise WbSerialConfigError(
            "Нет прав на чтение %s — сервис запущен не от root?" % path)
    except OSError as e:
        raise WbSerialConfigError(
            "Не удалось прочитать %s: %s" % (path, e))

    sha = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise WbSerialConfigError(
            "Файл %s не в UTF-8 (%s) — отредактируйте канал вручную" %
            (path, e))
    try:
        data = json.loads(text)
    except ValueError as e:
        raise WbSerialConfigError(
            "Конфиг %s не является чистым JSON (%s) — вероятно, в нём есть "
            "комментарии или JSON5. Автоматическая правка невозможна, "
            "включите канал Uptime вручную: Device Manager → устройство → "
            "HW Info → Uptime → in queue order" % (path, e))
    if not isinstance(data, dict):
        raise WbSerialConfigError(
            "Конфиг %s разобрался, но его корень — не объект" % path)
    return data, sha


def build_template_id_map(dirs) -> Dict[str, str]:
    """`{device_type: id_prefix}` из шаблонов драйвера.

    Нужна, чтобы вычислить MQTT-id устройства, у которого в конфиге не
    задан явный `id`: драйвер берёт `deviceID` из шаблона и добавляет
    `_<slave_id>` (наш `wb-map3e_21` — ровно такой случай).

    Нечитаемые/битые шаблоны молча пропускаются: это справочные данные,
    падать из-за одного кривого файла незачем.
    """
    id_map: Dict[str, str] = {}
    for d in (dirs or ()):
        try:
            names = sorted(glob.glob(os.path.join(d, "*.json")))
        except OSError:
            continue
        for name in names:
            try:
                with open(name, "r", encoding="utf-8") as f:
                    tpl = json.load(f)
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if not isinstance(tpl, dict):
                continue
            dev = tpl.get("device")
            dev = dev if isinstance(dev, dict) else {}
            dev_type = tpl.get("device_type") or dev.get("device_type")
            prefix = dev.get("id") or tpl.get("id")
            if dev_type and prefix:
                id_map.setdefault(str(dev_type), str(prefix))
    return id_map


def _iter_devices(data: dict):
    ports = data.get("ports")
    if not isinstance(ports, list):
        return
    for pi, port in enumerate(ports):
        if not isinstance(port, dict):
            continue
        devices = port.get("devices")
        if not isinstance(devices, list):
            continue
        for di, dev in enumerate(devices):
            if isinstance(dev, dict):
                yield pi, di, dev


def find_device(data: dict, device_id: str,
                id_map: Optional[Dict[str, str]] = None):
    """Ищет устройство в разобранном конфиге по MQTT device_id.

    Порядок строго такой (§3.3 ТЗ):
      1) явный `"id" == device_id`;
      2) вычисленный `<deviceID шаблона>_<slave_id>` — только для
         устройств без явного `id` (явный id имеет приоритет у драйвера);
      3) не нашлось ИЛИ нашлось больше одного → None. Гадать нельзя:
         лучше попросить человека, чем отредактировать чужое устройство.

    Возвращает (port_idx, dev_idx, device) либо None.
    """
    device_id = str(device_id or "")
    if not device_id:
        return None

    exact = [(pi, di, dev) for pi, di, dev in _iter_devices(data)
             if str(dev.get("id") or "") == device_id]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        log.warning("В конфиге драйвера несколько устройств с id=%r — "
                    "правку не предлагаем", device_id)
        return None

    id_map = id_map or {}
    computed = []
    for pi, di, dev in _iter_devices(data):
        if dev.get("id"):
            continue
        dev_type = dev.get("device_type")
        slave_id = dev.get("slave_id")
        if not dev_type or slave_id in (None, ""):
            continue
        prefix = id_map.get(str(dev_type))
        if not prefix:
            continue
        if "%s_%s" % (prefix, slave_id) == device_id:
            computed.append((pi, di, dev))
    if len(computed) == 1:
        return computed[0]
    if len(computed) > 1:
        log.warning("В конфиге драйвера несколько устройств дают id=%r — "
                    "правку не предлагаем", device_id)
    return None


def uptime_channel_state(device: dict) -> str:
    """"enabled" | "disabled" | "absent" — состояние канала Uptime в
    записи устройства. `absent` означает, что канал берётся из шаблона
    (в конфиге устройства его нет), а не что его нет вовсе."""
    channels = (device or {}).get("channels")
    if not isinstance(channels, list):
        return "absent"
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        if str(ch.get("name") or "") == UPTIME_CHANNEL:
            enabled = ch.get("enabled")
            if enabled is False:
                return "disabled"
            # Ключа нет — по README драйвера enabled по умолчанию true.
            return "enabled"
    return "absent"


# ---------------------------------------------------------------------
# Правка (в памяти) — точечная, §5.4
# ---------------------------------------------------------------------

def set_uptime_enabled(data: dict, port_idx: int, dev_idx: int) -> dict:
    """Возвращает НОВЫЙ документ, в котором у устройства
    ports[port_idx].devices[dev_idx] канал Uptime включён.

    Трогается только массив `channels` этого устройства: если запись
    Uptime есть — ей выставляется `enabled: true`, иначе добавляется
    `{"name": "Uptime", "enabled": true}`. Больше в документе не
    меняется ничего; порядок ключей сохраняется сам (json в Python 3.7+
    держит порядок вставки).
    """
    import copy

    new_data = copy.deepcopy(data)
    try:
        device = new_data["ports"][port_idx]["devices"][dev_idx]
    except (KeyError, IndexError, TypeError):
        raise WbSerialConfigError(
            "Устройство ports[%s].devices[%s] не найдено в конфиге" %
            (port_idx, dev_idx))
    if not isinstance(device, dict):
        raise WbSerialConfigError(
            "Запись устройства ports[%s].devices[%s] — не объект" %
            (port_idx, dev_idx))

    channels = device.get("channels")
    if not isinstance(channels, list):
        if channels is not None:
            raise WbSerialConfigError(
                "У устройства поле channels не является массивом — "
                "правка отменена")
        channels = []
        device["channels"] = channels

    for ch in channels:
        if isinstance(ch, dict) and str(ch.get("name") or "") == UPTIME_CHANNEL:
            ch["enabled"] = True
            return new_data

    channels.append({"name": UPTIME_CHANNEL, "enabled": True})
    return new_data


# ---------------------------------------------------------------------
# Бэкапы
# ---------------------------------------------------------------------

def _backup_prefix(path: str) -> str:
    return os.path.basename(path) + "."


def make_backup(path: str, backup_dir: str) -> str:
    """Копия конфига в backup_dir с меткой времени. Делается ТОЛЬКО
    когда дело реально дошло до записи (§5.2)."""
    try:
        os.makedirs(backup_dir, exist_ok=True)
    except OSError as e:
        raise WbSerialConfigError(
            "Не удалось создать каталог бэкапов %s: %s" % (backup_dir, e))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(backup_dir, _backup_prefix(path) + stamp)
    dest = base + ".bak"
    n = 1
    while os.path.exists(dest):
        dest = "%s-%d.bak" % (base, n)
        n += 1
    try:
        shutil.copy2(path, dest)
    except OSError as e:
        raise WbSerialConfigError(
            "Не удалось создать бэкап %s: %s" % (dest, e))
    return dest


def list_backups(path: str, backup_dir: str) -> List[str]:
    """Бэкапы конфига `path` в `backup_dir`, от старых к новым."""
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return []
    prefix = _backup_prefix(path)
    items = [os.path.join(backup_dir, n) for n in names
             if n.startswith(prefix) and n.endswith(".bak")]
    return sorted(items)


def prune_backups(path: str, backup_dir: str,
                  keep: int = KEEP_BACKUPS) -> List[str]:
    """Оставить последние `keep` бэкапов, остальные удалить.
    Возвращает список удалённых."""
    items = list_backups(path, backup_dir)
    removed = []
    for old in items[:max(0, len(items) - keep)]:
        try:
            os.unlink(old)
            removed.append(old)
        except OSError as e:
            log.warning("Не удалось удалить старый бэкап %s: %s", old, e)
    return removed


def latest_backup(path: str, backup_dir: str) -> Optional[str]:
    items = list_backups(path, backup_dir)
    return items[-1] if items else None


def restore_backup(backup_path: str, path: str) -> None:
    """Вернуть конфиг из бэкапа — тоже атомарно, чтобы откат не оставил
    драйверу обрезанный файл."""
    with open(backup_path, "rb") as f:
        payload = f.read()
    _atomic_write_bytes(path, payload)


# ---------------------------------------------------------------------
# Атомарная запись (§5.5)
# ---------------------------------------------------------------------

def _atomic_write_bytes(path: str, payload: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        st = os.stat(path)
    except OSError:
        st = None
    tmp_path = os.path.join(
        directory, ".%s.wbem-tmp.%d" % (os.path.basename(path), os.getpid()))
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        if st is not None:
            try:
                os.chmod(tmp_path, stat.S_IMODE(st.st_mode))
            except OSError as e:
                log.warning("Не удалось сохранить права файла %s: %s", path, e)
            chown = getattr(os, "chown", None)
            if chown is not None:
                try:
                    chown(tmp_path, st.st_uid, st.st_gid)
                except OSError as e:
                    # Не root — права всё равно сохранены, владелец нет.
                    log.warning("Не удалось сохранить владельца %s: %s",
                                path, e)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise
    # Сброс каталога — чтобы переименование пережило внезапную потерю
    # питания на контроллере (SD-карта).
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def dump_config(data: dict) -> bytes:
    """Сериализация конфига: indent=4, ensure_ascii=False (§5.8) —
    иначе русские имена устройств превратятся в \\uXXXX и человек не
    узнает свой файл."""
    text = json.dumps(data, indent=4, ensure_ascii=False)
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def save_config(path: str, data: dict, expected_sha: str, backup_dir: str,
                keep: int = KEEP_BACKUPS, verify=None) -> dict:
    """Запись конфига драйвера со всеми предохранителями §5.

    Порядок принципиален:
      1) перечитать файл и сверить sha256 (§5.3) — не совпал, значит
         кто-то (веб-интерфейс WB) правил файл, отказ БЕЗ записи;
      2) только теперь бэкап (§5.2);
      3) атомарная запись с сохранением прав/владельца (§5.5);
      4) перечитать и проверить (§5.6) — при любой неожиданности
         немедленный откат из бэкапа и исключение.

    `verify` — необязательный `callable(data) -> bool` для проверки, что
    наше изменение действительно на месте.

    Возвращает {"sha256", "backup_path", "pruned"}.
    """
    # 1. Оптимистическая блокировка. load_config заодно проверит, что
    # файл всё ещё чистый JSON.
    current_data, current_sha = load_config(path)
    if expected_sha and current_sha != expected_sha:
        raise WbSerialConflict(
            "Конфиг %s изменился с момента чтения (кто-то правил его "
            "через веб-интерфейс WB?) — обновите страницу и повторите" % path)
    del current_data

    payload = dump_config(data)

    # 2. Бэкап — только сейчас, когда мы точно собираемся писать.
    backup_path = make_backup(path, backup_dir)

    # 3. Атомарная запись.
    try:
        _atomic_write_bytes(path, payload)
    except OSError as e:
        raise WbSerialConfigError(
            "Не удалось записать %s: %s (файл не изменён, бэкап: %s)" %
            (path, e, backup_path))

    # 4. Проверка после записи, при любой беде — откат.
    try:
        new_data, new_sha = load_config(path)
        if verify is not None and not verify(new_data):
            raise WbSerialConfigError(
                "После записи изменение не обнаружено в файле")
    except WbSerialConfigError as e:
        try:
            restore_backup(backup_path, path)
            restored = "конфиг восстановлен из бэкапа %s" % backup_path
        except OSError as e2:
            restored = ("ВОССТАНОВИТЬ ИЗ БЭКАПА НЕ УДАЛОСЬ (%s), "
                        "бэкап лежит здесь: %s" % (e2, backup_path))
        raise WbSerialConfigError(
            "Проверка после записи не прошла: %s. %s" % (e, restored))

    pruned = prune_backups(path, backup_dir, keep=keep)
    log.info("Конфиг драйвера обновлён: %s (бэкап %s)", path, backup_path)
    return {"sha256": new_sha, "backup_path": backup_path, "pruned": pruned}


# ---------------------------------------------------------------------
# Перезапуск драйвера (§4.2) — только по явной кнопке
# ---------------------------------------------------------------------

def _default_runner(cmd, timeout=None):
    return subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)


def restart_service(service_name: str, timeout_s: float = 30.0,
                    runner=None, sleep=time.sleep) -> Tuple[bool, str]:
    """`systemctl restart <service>` + ожидание `is-active` до timeout_s.

    Имя юнита берётся из конфига сервиса, не из HTTP-запроса, и всё
    равно проверяется регуляркой — в командную строку не должно попасть
    ничего необычного. Возвращает (ok, человеческое описание).
    """
    if not _SERVICE_NAME_RE.match(str(service_name or "")):
        return False, ("Недопустимое имя юнита в конфиге сервиса: %r" %
                       service_name)
    run = runner or _default_runner
    try:
        res = run(["systemctl", "restart", str(service_name)],
                  timeout=max(5.0, timeout_s))
    except FileNotFoundError:
        return False, "systemctl не найден — это не systemd-система"
    except subprocess.TimeoutExpired:
        return False, "systemctl restart не завершился за отведённое время"
    except OSError as e:
        return False, "Не удалось выполнить systemctl restart: %s" % e

    rc = getattr(res, "returncode", 1)
    out = getattr(res, "stdout", b"") or b""
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    if rc != 0:
        return False, ("systemctl restart %s вернул код %s: %s" %
                       (service_name, rc, out.strip()[:300]))

    deadline = time.time() + timeout_s
    last = ""
    while True:
        try:
            res = run(["systemctl", "is-active", str(service_name)],
                      timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            last = str(e)
        else:
            out = getattr(res, "stdout", b"") or b""
            if isinstance(out, bytes):
                out = out.decode("utf-8", errors="replace")
            last = out.strip()
            if last == "active":
                return True, "active"
        if time.time() >= deadline:
            return False, ("Драйвер %s не поднялся за %d с (последнее "
                           "состояние: %s)" %
                           (service_name, int(timeout_s), last or "неизвестно"))
        sleep(1.0)


# ---------------------------------------------------------------------
# Диагностика: сведение конфига и MQTT в один вердикт (§3.2)
# ---------------------------------------------------------------------

UPTIME_FRESH_S = 60.0

_DETAILS = {
    "ok": "Канал Uptime опрашивается, счётчик подтверждает связь каждые "
          "несколько секунд.",
    "disabled": "В конфиге драйвера у канала Uptime стоит enabled: false — "
                "счётчик не подаёт признаков жизни, пока не меняется "
                "нагрузка. Включите канал: Device Manager → устройство → "
                "HW Info → Uptime → in queue order.",
    "not_in_config": "Канал Uptime не задан в конфиге устройства и данные по "
                     "нему не идут. Включите его: Device Manager → "
                     "устройство → HW Info → Uptime → in queue order.",
    "stale": "Канал Uptime в конфиге включён, но данные по нему не приходят "
             "дольше минуты — это уже похоже на проблему связи с "
             "устройством, а не на конфиг.",
    "device_not_found": "Устройство не найдено в /etc/wb-mqtt-serial.conf "
                        "(или найдено больше одного подходящего) — счётчик "
                        "опрашивается не через этот конфиг либо id не "
                        "совпал. Правку не предлагаем.",
    "unknown": "Проверить состояние канала не удалось: конфиг драйвера не "
               "прочитался.",
}


def mqtt_uptime_state(meter, now: Optional[float] = None) -> str:
    """"ok" | "stale" | "never" по данным MQTT (MeterState.controls)."""
    if meter is None:
        return "never"
    controls = getattr(meter, "controls", None) or {}
    ctrl = controls.get(UPTIME_CHANNEL)
    if ctrl is None:
        return "never"
    count = getattr(ctrl, "update_count", 0) or 0
    last = getattr(ctrl, "last_update_ts", 0) or 0
    if count < 2 or last <= 0:
        return "never"
    now = time.time() if now is None else now
    return "ok" if (now - last) <= UPTIME_FRESH_S else "stale"


def verdict(config_state: Optional[str], mqtt_state: str) -> str:
    """Сводит два независимых источника правды в один из шести статусов
    таблицы §3.2. `config_state`: "enabled"/"disabled"/"absent",
    None — устройство не найдено, "unknown" — конфиг не прочитался."""
    if config_state == "unknown":
        return "unknown"
    if config_state is None:
        return "device_not_found"
    if mqtt_state == "ok":
        return "ok"
    if config_state == "disabled":
        return "disabled"
    if config_state == "absent":
        return "not_in_config"
    return "stale"


def detail_text(state: str) -> str:
    return _DETAILS.get(state, "")


def describe_meter(meter, device_id: str, data: Optional[dict],
                   id_map: Optional[Dict[str, str]],
                   config_error: Optional[str] = None,
                   allow_edit: bool = False,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """Полный вердикт по одному счётчику. `data=None` + `config_error` —
    конфиг не прочитался (это НЕ ошибка интерфейса: файла может не быть
    на машине разработчика, в тестах, в контейнере)."""
    mqtt_state = mqtt_uptime_state(meter, now=now)
    found = None
    if data is None:
        config_state: Optional[str] = "unknown"
    else:
        found = find_device(data, device_id, id_map or {})
        config_state = uptime_channel_state(found[2]) if found else None
    state = verdict(config_state, mqtt_state)
    can_enable = bool(allow_edit and found is not None and
                      state in ("disabled", "not_in_config"))
    out = {
        "device_id": device_id,
        "state": state,
        "config_state": config_state,
        "mqtt_state": mqtt_state,
        "can_enable": can_enable,
        "allow_edit": bool(allow_edit),
        "detail": detail_text(state),
    }
    if state == "unknown" and config_error:
        out["detail"] = "%s %s" % (detail_text("unknown"), config_error)
        out["config_error"] = config_error
    return out

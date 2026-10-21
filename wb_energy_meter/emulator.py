"""Эмулятор счётчиков (П12, решение R17, проектная заметка docs/design-emulator.md).

Виртуальные WB-MAP3E в том же брокере: на контроллере без нагрузки иначе нечем
проверять учёт. Эмулятор публикует топики так, как их публикует драйвер
`wb-mqtt-serial`, а историю пишет настоящий `wb-mqtt-db`; весь остальной
сервис видит обычные приборы (с префиксом `emu-`) и знает о них только то, что
они «эмулятор».

Главная мысль — накопитель это **функция времени**, а не переменная:

    E_фаза(t) = anchor_kwh[фаза] + ∫ P_фаза(τ) dτ / 3600,  τ ∈ [anchor_ts, t]

Мощность кусочно-постоянна по минутам и детерминированно зависит от настроек
прибора. Поэтому после перезапуска счётчик продолжается ровно с того же места
(якорь лежит в kv), а на диск пишется только якорь — при правке, неисправности
и раз в час, а не каждые 5 секунд (флеш контроллера).
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import zlib
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

EMU_PREFIX = "emu-"
EMU_DRIVER = "wb-energy-meter-emulator"
STATE_KEY = "emulator.state"
JOURNAL_KEY = "emulator.journal"
JOURNAL_MAX = 200

PHASES = ("L1", "L2", "L3")
TICK_S = 5.0
REANCHOR_S = 3600.0               # якорь в kv обновляется раз в час
HISTORY_PROBE_DELAY_S = 12 * 60   # П12: через 10–15 минут после включения
HISTORY_PROBE_RETRY_S = 10 * 60
PROBE_CONTROL = "Total AP energy"
MAX_DEVICES = 12
MAX_KW = 100.0
NOISE = 0.05                      # ±5 % к мощности
POWER_FACTOR = 0.95
NOMINAL_V = 230.0
SPARSE_MIN_MIN, SPARSE_SPAN_MIN = 30, 31   # 30–60 минут между показаниями
LOSS_MAX_MIN, SPARSE_MAX_H = 24 * 60, 72

HISTORY_WARNING = ("wb-mqtt-db не пишет историю эмулятора — отчёты будут "
                   "пустыми; проверьте настройки wb-mqtt-db (группы каналов)")
HISTORY_ERROR = "не удалось спросить wb-mqtt-db: "

PROFILES = ("flat", "daily")
ROLES = ("load", "input")
FAULT_KINDS = ("reset", "replace", "loss", "sparse")


class EmulatorError(Exception):
    """Ошибка запроса к эмулятору; `status` — HTTP-код для API."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Идентификаторы и имена
# ---------------------------------------------------------------------------

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def is_emulator_device(device_id) -> bool:
    return isinstance(device_id, str) and device_id.startswith(EMU_PREFIX)


def plural_devices(n: int) -> str:
    """«1 виртуальный прибор», «2 виртуальных прибора», «5 виртуальных приборов»."""
    if n % 10 == 1 and n % 100 != 11:
        return "виртуальный прибор"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "виртуальных прибора"
    return "виртуальных приборов"


def device_id_for(n: int) -> str:
    return "%smap3e-%d" % (EMU_PREFIX, n)


def serial_for(n: int, gen: int) -> str:
    return "EMU-%d-%d" % (n, gen)


def normalize_name(name, n: int) -> str:
    """Название прибора. Слово «эмулятор» остаётся в нём всегда: тогда метка
    видна в любом месте, где показано имя, в том числе в устаревших экранах."""
    s = " ".join(str(name or "").split())
    if _CTRL_RE.search(s):
        raise EmulatorError("название содержит управляющие символы")
    if not s:
        return "Эмулятор %d" % n
    if len(s) > 60:
        raise EmulatorError("название длиннее 60 символов")
    if "эмулятор" not in s.lower():
        s = "Эмулятор: " + s
    return s


# ---------------------------------------------------------------------------
# Модель мощности (чистые функции)
# ---------------------------------------------------------------------------

def _u(key: str) -> float:
    """Детерминированное «случайное» число в [-1, 1) по ключу (без random:
    результат одинаков в любом процессе и при любом перезапуске)."""
    return (zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF) / 2147483648.0 - 1.0


def profile_factor(profile: str, sec_of_day: float) -> float:
    """Множитель суточного профиля: ночью ≈ 0,3, днём до 1 (день > ночи)."""
    if profile != "daily":
        return 1.0
    h = (sec_of_day % 86400.0) / 3600.0
    if h < 5.5 or h >= 23.0:
        return 0.30
    return 0.30 + 0.70 * math.sin(math.pi * (h - 5.5) / 17.5) ** 2


class EmuDevice:
    """Настройки и якорь одного виртуального прибора."""

    def __init__(self, n, name, kw=(0.0, 0.0, 0.0), profile="daily",
                 role="load", sources=None, pct=0.0, gen=0, anchor_ts=0.0,
                 anchor_kwh=(0.0, 0.0, 0.0), started_ts=0.0,
                 loss_until=None, sparse_until=None, next_sparse_ts=0.0):
        self.n = int(n)
        self.name = name
        self.kw = [float(x) for x in kw]
        self.profile = profile
        self.role = role
        self.sources = [int(x) for x in (sources or [])]
        self.pct = float(pct)
        self.gen = int(gen)
        self.anchor_ts = float(anchor_ts)
        self.anchor_kwh = [float(x) for x in anchor_kwh]
        self.started_ts = float(started_ts)
        self.loss_until = loss_until
        self.sparse_until = sparse_until
        self.next_sparse_ts = float(next_sparse_ts or 0.0)

    @property
    def device_id(self):
        return device_id_for(self.n)

    @property
    def serial(self):
        return serial_for(self.n, self.gen)

    def to_dict(self):
        return {
            "n": self.n, "name": self.name, "kw": self.kw,
            "profile": self.profile, "role": self.role,
            "sources": self.sources, "pct": self.pct, "gen": self.gen,
            "anchor_ts": self.anchor_ts, "anchor_kwh": self.anchor_kwh,
            "started_ts": self.started_ts, "loss_until": self.loss_until,
            "sparse_until": self.sparse_until,
            "next_sparse_ts": self.next_sparse_ts,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            d["n"], d.get("name") or "Эмулятор %d" % d["n"],
            kw=d.get("kw") or (0, 0, 0), profile=d.get("profile", "daily"),
            role=d.get("role", "load"), sources=d.get("sources"),
            pct=d.get("pct", 0.0), gen=d.get("gen", 0),
            anchor_ts=d.get("anchor_ts", 0.0),
            anchor_kwh=d.get("anchor_kwh") or (0, 0, 0),
            started_ts=d.get("started_ts", 0.0),
            loss_until=d.get("loss_until"), sparse_until=d.get("sparse_until"),
            next_sparse_ts=d.get("next_sparse_ts", 0.0))


def control_specs():
    """Каналы WB-MAP3E: (имя, тип, единицы). Мощность в Вт, энергия в кВт·ч —
    как у настоящего прибора и как в репетиции пилота."""
    out = [("Total AP energy", "value", "kWh"), ("Total P", "power", "W"),
           ("Frequency", "value", "Hz")]
    for ph in PHASES:
        out += [("AP energy " + ph, "value", "kWh"), ("P " + ph, "power", "W"),
                ("Urms " + ph, "voltage", "V"), ("Irms " + ph, "current", "A")]
    out += [("Serial", "text", None), ("Uptime", "value", "s")]
    return out


CONTROL_NAMES = tuple(c[0] for c in control_specs())


def device_topics(device_id: str) -> List[str]:
    """Все топики прибора — по одному device_id, без настроек: стереть
    retained можно и у уже удалённого прибора."""
    base = "/devices/%s" % device_id
    out = [base + "/meta", base + "/meta/name"]
    for c in CONTROL_NAMES:
        out += ["%s/controls/%s" % (base, c), "%s/controls/%s/meta" % (base, c),
                "%s/controls/%s/meta/error" % (base, c)]
    return out


# ---------------------------------------------------------------------------
# Публикатор в брокер
# ---------------------------------------------------------------------------

class PahoPublisher:
    """Свой клиент paho (не `MqttService`): сетевой цикл запущен до публикации,
    `wait_for_publish(timeout=…)` не используется (старый paho на контроллере
    не знает этого аргумента, см. CODING_STANDARDS)."""

    def __init__(self, broker, port, username=None, password=None,
                 client_id_prefix="wb-energy-meter-emu"):
        import uuid
        import paho.mqtt.client as mqtt
        self._connected = threading.Event()
        self._infos = []
        cid = "%s-%s" % (client_id_prefix, uuid.uuid4().hex[:8])
        self._client = mqtt.Client(client_id=cid, clean_session=True)
        if username:
            self._client.username_pw_set(username, password or "")
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._broker, self._port = broker, port

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected.set()

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def connect(self, timeout_s=5.0) -> bool:
        try:
            self._client.connect_async(self._broker, self._port, 30)
        except Exception as e:
            log.warning("Эмулятор: MQTT connect_async: %s", e)
            return False
        self._client.loop_start()
        return self._connected.wait(timeout=timeout_s)

    def publish(self, topic, payload, retain=True, qos=1):
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        self._infos.append(info)
        if len(self._infos) > 5000:
            self._infos = [i for i in self._infos if not i.is_published()]

    def flush(self, timeout_s=5.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if all(i.is_published() for i in self._infos):
                self._infos = []
                return True
            time.sleep(0.02)
        return False

    def close(self):
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Сервис
# ---------------------------------------------------------------------------

class EmulatorService:
    """Включение/выключение, приборы, сценарии, журнал, публикация.

    Зависимости подменяются: `kv` (get/set/delete), `publisher_factory()` →
    объект с connect/publish/flush/close/connected, `clock`, `history_probe(
    device_id, control, ts_from) -> число точек истории`, `registry` и
    `meters_repo` (забывать приборы без привязки при выключении).
    """

    def __init__(self, kv, publisher_factory=None, registry=None,
                 meters_repo=None, history_probe=None, clock=time.time,
                 utc_offset_s=None, tick_s=TICK_S,
                 probe_delay_s=HISTORY_PROBE_DELAY_S,
                 probe_retry_s=HISTORY_PROBE_RETRY_S):
        self._kv = kv
        self._factory = publisher_factory
        self._registry = registry
        self._meters_repo = meters_repo
        self._probe = history_probe
        self._clock = clock
        self._tick_s = tick_s
        self._probe_delay = probe_delay_s
        self._probe_retry = probe_retry_s
        if utc_offset_s is None:
            utc_offset_s = time.localtime().tm_gmtoff or 0
        self._utc = int(utc_offset_s)
        self._lock = threading.RLock()
        self._publisher = None
        self._last: Dict[tuple, str] = {}
        self._cache: Dict[int, tuple] = {}
        self._stop = threading.Event()
        self._thread = None
        self._probe_thread = None
        self._warned_at = 0.0
        # состояние (kv)
        self._enabled = False
        self._enabled_at = None
        self._next_n = 1
        self._devices: Dict[int, EmuDevice] = {}
        self._history = {"state": "pending", "checked_at": None, "message": ""}
        self._next_probe_ts = 0.0
        self._pending_erase: List[str] = []
        self._journal: List[dict] = []
        self._load()

    # --- хранение ---------------------------------------------------------

    def _load(self):
        st = self._kv.get(STATE_KEY, None) if self._kv else None
        if isinstance(st, dict):
            self._enabled = bool(st.get("enabled"))
            self._enabled_at = st.get("enabled_at")
            self._next_n = int(st.get("next_n", 1))
            self._devices = {}
            for d in st.get("devices") or []:
                try:
                    dev = EmuDevice.from_dict(d)
                    self._devices[dev.n] = dev
                except Exception as e:
                    log.warning("Эмулятор: прибор в kv пропущен: %s", e)
            hist = st.get("history")
            if isinstance(hist, dict):
                self._history.update(hist)
            self._pending_erase = list(st.get("pending_erase") or [])
        jr = self._kv.get(JOURNAL_KEY, None) if self._kv else None
        if isinstance(jr, list):
            self._journal = jr[-JOURNAL_MAX:]

    def _persist(self):
        if not self._kv:
            return
        self._kv.set(STATE_KEY, {
            "v": 1, "enabled": self._enabled, "enabled_at": self._enabled_at,
            "next_n": self._next_n,
            "devices": [d.to_dict() for d in self._sorted()],
            "history": self._history, "pending_erase": self._pending_erase,
        })

    def _log(self, kind, text, device_id=None):
        self._journal.append({"ts": int(self._clock()), "kind": kind,
                              "device_id": device_id, "text": text})
        del self._journal[:-JOURNAL_MAX]
        if self._kv:
            self._kv.set(JOURNAL_KEY, self._journal)

    def _sorted(self):
        return [self._devices[k] for k in sorted(self._devices)]

    # --- мощность и энергия -----------------------------------------------

    def _power_kw(self, dev, pi, bucket):
        """Мощность фазы `pi` в минутном «ведре» `bucket`, кВт."""
        if dev.role == "input":
            s = 0.0
            for n in dev.sources:
                src = self._devices.get(n)
                if src is not None and src.role == "load":
                    s += self._power_kw(src, pi, bucket)
            return (1.0 + dev.pct / 100.0) * s
        sec = bucket * 60 + 30 + self._utc
        f = profile_factor(dev.profile, sec)
        return dev.kw[pi] * f * (1.0 + NOISE * _u("%d:%d:%d" % (dev.n, pi, bucket)))

    def _integral_kwh(self, dev, pi, t0, t1):
        if t1 <= t0:
            return 0.0
        total, t, b = 0.0, t0, int(t0 // 60)
        while t < t1:
            end = min((b + 1) * 60, t1)
            total += self._power_kw(dev, pi, b) * (end - t)
            t, b = end, b + 1
        return total / 3600.0

    def energies(self, dev, t):
        """Показания накопителей L1–L3 на момент `t` (кВт·ч)."""
        with self._lock:
            if t <= dev.anchor_ts:
                return list(dev.anchor_kwh)
            hit = self._cache.get(dev.n)
            if hit is not None and dev.anchor_ts <= hit[0] <= t:
                base_ts, base = hit
            else:
                base_ts, base = dev.anchor_ts, dev.anchor_kwh
            out = [base[i] + self._integral_kwh(dev, i, base_ts, t)
                   for i in range(3)]
            self._cache[dev.n] = (t, out)
            return list(out)

    def power_w(self, dev, t):
        bucket = int(t // 60)
        return [self._power_kw(dev, i, bucket) * 1000.0 for i in range(3)]

    def _reanchor_device(self, dev, now):
        dev.anchor_kwh = self.energies(dev, now)
        dev.anchor_ts = now
        self._cache.pop(dev.n, None)

    def _reanchor_all(self, now):
        """Перед любой правкой настроек: прошлое считается по старым настройкам."""
        for dev in self._devices.values():
            self._reanchor_device(dev, now)
        self._cache.clear()

    # --- жизненный цикл ----------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="emulator",
                                        daemon=True)
        self._thread.start()
        log.info("Эмулятор счётчиков: сервис запущен (включён: %s, приборов: %d)",
                 self._enabled, len(self._devices))

    def stop(self):
        """Остановка сервиса. Топики НЕ стираются: при быстром перезапуске
        значения остаются в брокере; стирает только выключение."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        with self._lock:
            if self._enabled and self._devices:
                self._reanchor_all(self._clock())
                self._persist()
            self._close_publisher()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # сбой тика не должен убить поток
                log.exception("Эмулятор: ошибка тика: %s", e)
            self._stop.wait(self._tick_s)

    def _get_publisher(self):
        if self._publisher is None and self._factory is not None:
            pub = self._factory()
            pub.connect()
            self._publisher = pub
        return self._publisher

    def _close_publisher(self):
        pub, self._publisher = self._publisher, None
        if pub is not None:
            try:
                pub.flush(2.0)
            except Exception:
                pass
            pub.close()

    # --- включение / выключение --------------------------------------------

    def is_enabled(self):
        return self._enabled

    def set_enabled(self, flag: bool):
        flag = bool(flag)
        with self._lock:
            now = self._clock()
            if flag == self._enabled:
                return
            if flag:
                # выключенный эмулятор «стоит»: якорь переносится на момент
                # включения, счётчики не прыгают за время простоя
                for dev in self._devices.values():
                    dev.anchor_ts = now
                    dev.started_ts = now
                    dev.loss_until = dev.sparse_until = None
                self._cache.clear()
                self._enabled, self._enabled_at = True, now
                self._pending_erase = [d for d in self._pending_erase
                                       if d not in {x.device_id for x in
                                                    self._devices.values()}]
                self._history = {"state": "pending", "checked_at": None,
                                 "message": ""}
                self._next_probe_ts = 0.0
                self._last.clear()
                self._log("enabled", "Эмулятор включён")
                self._persist()
            else:
                self._reanchor_all(now)
                ids = [d.device_id for d in self._sorted()]
                self._enabled = False
                for dev in self._devices.values():
                    dev.loss_until = dev.sparse_until = None
                self._log("disabled", "Эмулятор выключен: публикация "
                          "остановлена, retained-топики стёрты")
                self._pending_erase = sorted(set(self._pending_erase) | set(ids))
                self._persist()
                self._erase_pending()
        if flag:
            self.tick()

    def _erase_pending(self):
        """Стереть retained-топики приборов из `pending_erase`. Если брокер
        недоступен, список остаётся в kv и стирание повторится на ближайшем
        тике — «фантомные» приборы не переживут перезапуск."""
        if not self._pending_erase:
            return
        pub = self._get_publisher()
        if pub is None or not pub.connected:
            return
        done = []
        for did in list(self._pending_erase):
            for topic in device_topics(did):
                pub.publish(topic, "", retain=True, qos=1)
            done.append(did)
        pub.flush(5.0)
        self._pending_erase = [d for d in self._pending_erase if d not in done]
        for k in [k for k in self._last if k[0] in done]:
            self._last.pop(k, None)
        self._forget_unbound(done)
        self._persist()
        if not self._enabled:
            self._close_publisher()

    def _forget_unbound(self, device_ids):
        if self._registry is None:
            return
        for did in device_ids:
            bound = False
            if self._meters_repo is not None:
                try:
                    bound = self._meters_repo.get_by_device_id(did) is not None
                except Exception:
                    bound = True
            if not bound:
                self._registry.forget(did)

    # --- приборы -------------------------------------------------------------

    @staticmethod
    def _check_kw(kw):
        if (not isinstance(kw, (list, tuple)) or len(kw) != 3):
            raise EmulatorError("kw: нужны три числа — мощность L1, L2, L3, кВт")
        out = []
        for x in kw:
            if isinstance(x, bool) or not isinstance(x, (int, float)) \
                    or not math.isfinite(x) or x < 0 or x > MAX_KW:
                raise EmulatorError("kw: число от 0 до %g кВт на каждую фазу"
                                    % MAX_KW)
            out.append(float(x))
        return out

    def _check_fields(self, spec, dev=None):
        """Проверка полей прибора; для PATCH (dev задан) — только переданные."""
        out = {}
        if "profile" in spec:
            if spec["profile"] not in PROFILES:
                raise EmulatorError("profile: flat или daily")
            out["profile"] = spec["profile"]
        if "role" in spec:
            if spec["role"] not in ROLES:
                raise EmulatorError("role: load или input")
            out["role"] = spec["role"]
        if "kw" in spec:
            out["kw"] = self._check_kw(spec["kw"])
        if "pct" in spec:
            p = spec["pct"]
            if isinstance(p, bool) or not isinstance(p, (int, float)) \
                    or not math.isfinite(p) or p < -50 or p > 200:
                raise EmulatorError("pct: число от -50 до 200")
            out["pct"] = float(p)
        if "sources" in spec:
            src = spec["sources"]
            if not isinstance(src, list) or any(
                    isinstance(x, bool) or not isinstance(x, int) for x in src):
                raise EmulatorError("sources: список номеров приборов")
            for n in src:
                s = self._devices.get(n)
                if s is None:
                    raise EmulatorError("sources: прибора %d нет" % n)
                if s.role != "load":
                    raise EmulatorError(
                        "sources: прибор %d сам является вводом" % n)
                if dev is not None and n == dev.n:
                    raise EmulatorError("sources: ввод не может питаться от себя")
            out["sources"] = sorted(set(src))
        return out

    def add_device(self, spec):
        spec = spec or {}
        with self._lock:
            if len(self._devices) >= MAX_DEVICES:
                raise EmulatorError("приборов не больше %d" % MAX_DEVICES, 409)
            fields = self._check_fields(spec)
            role = fields.get("role", "load")
            if role == "input" and not fields.get("sources"):
                raise EmulatorError("для ввода укажите sources — нагрузки, "
                                    "которые он суммирует")
            if role == "load" and "kw" not in fields:
                raise EmulatorError("kw: нужны три числа — мощность L1, L2, L3, кВт")
            now = self._clock()
            n = self._next_n
            name = normalize_name(spec.get("name"), n)
            self._reanchor_all(now)
            dev = EmuDevice(
                n, name, kw=fields.get("kw", (0, 0, 0)),
                profile=fields.get("profile", "daily"), role=role,
                sources=fields.get("sources"), pct=fields.get("pct", 0.0),
                anchor_ts=now, started_ts=now)
            self._devices[n] = dev
            self._next_n = n + 1
            self._log("device_added", "Добавлен прибор «%s» (%s)"
                      % (name, dev.device_id), dev.device_id)
            self._persist()
            return dev

    def add_preset(self):
        """Типовой набор: три нагрузки с разными фазами и ввод над ними."""
        loads = [
            ("нагрузка A", [1.2, 0.4, 0.8], "daily"),
            ("нагрузка B", [0.3, 2.0, 0.5], "daily"),
            ("нагрузка C", [0.6, 0.6, 1.8], "flat"),
        ]
        with self._lock:
            if len(self._devices) + 4 > MAX_DEVICES:
                raise EmulatorError("не хватает места: приборов не больше %d"
                                    % MAX_DEVICES, 409)
            made = []
            for title, kw, prof in loads:
                n = self._next_n
                made.append(self.add_device(
                    {"name": "Эмулятор %d: %s" % (n, title), "kw": kw,
                     "profile": prof}))
            n = self._next_n
            made.append(self.add_device(
                {"name": "Эмулятор %d: ввод" % n, "role": "input",
                 "sources": [d.n for d in made], "pct": 3.0}))
            return made

    def update_device(self, n, patch):
        patch = patch or {}
        editable = ("name", "kw", "profile", "role", "sources", "pct")
        if not any(k in patch for k in editable):
            raise EmulatorError("нечего менять: укажите name, kw, profile, "
                                "role, sources или pct")
        with self._lock:
            dev = self._get(n)
            fields = self._check_fields(patch, dev)
            role = fields.get("role", dev.role)
            sources = fields.get("sources", dev.sources)
            if role == "input" and not sources:
                raise EmulatorError("для ввода укажите sources")
            if role == "load" and dev.role == "input" and "kw" not in fields:
                raise EmulatorError("при смене роли на нагрузку укажите kw")
            if role == "input" and dev.role == "load" and any(
                    dev.n in d.sources for d in self._devices.values()):
                raise EmulatorError(
                    "прибор питает ввод; уберите его из sources ввода", 409)
            name = normalize_name(patch["name"], n) if "name" in patch else None
            self._reanchor_all(self._clock())
            for k, v in fields.items():
                setattr(dev, k, v)
            if name is not None:
                dev.name = name
            self._persist()
            return dev

    def remove_device(self, n):
        with self._lock:
            dev = self._get(n)
            self._reanchor_all(self._clock())
            del self._devices[n]
            self._cache.pop(n, None)
            for d in self._devices.values():
                if n in d.sources:
                    d.sources = [x for x in d.sources if x != n]
            self._log("device_removed", "Удалён прибор «%s» (%s)"
                      % (dev.name, dev.device_id), dev.device_id)
            if self._enabled:
                self._pending_erase = sorted(set(self._pending_erase)
                                             | {dev.device_id})
                self._persist()
                self._erase_pending()
            else:
                self._persist()
            self._forget_unbound([dev.device_id])
            return dev

    def _get(self, n):
        dev = self._devices.get(n)
        if dev is None:
            raise EmulatorError("прибора %s нет" % n, 404)
        return dev

    # --- неисправности ---------------------------------------------------------

    def apply_fault(self, n, kind, amount=None):
        """Сброс, замена, потеря связи (минуты), редкие показания (часы).
        Каждое действие пишется в журнал со временем."""
        if kind not in FAULT_KINDS:
            raise EmulatorError("kind: reset, replace, loss или sparse")
        with self._lock:
            dev = self._get(n)
            if not self._enabled:
                raise EmulatorError("эмулятор выключен: включите его, "
                                    "чтобы вызывать неисправности", 409)
            now = self._clock()
            did = dev.device_id
            if kind == "reset":
                dev.anchor_ts, dev.anchor_kwh = now, [0.0, 0.0, 0.0]
                self._cache.pop(n, None)
                self._log("reset", "Сброс счётчика «%s»: показания обнулены"
                          % dev.name, did)
            elif kind == "replace":
                dev.gen += 1
                dev.anchor_ts, dev.anchor_kwh = now, [0.0, 0.0, 0.0]
                dev.started_ts = now
                self._cache.pop(n, None)
                self._log("replace", "Замена счётчика «%s»: новый серийный %s, "
                          "показания с нуля" % (dev.name, dev.serial), did)
            elif kind == "loss":
                minutes = self._amount(amount, 1, LOSS_MAX_MIN, "minutes",
                                       "минут")
                dev.loss_until = now + minutes * 60
                self._log("loss", "Потеря связи с «%s» на %d мин (meta/error = r)"
                          % (dev.name, minutes), did)
            else:
                hours = self._amount(amount, 1, SPARSE_MAX_H, "hours", "часов")
                dev.sparse_until = now + hours * 3600
                dev.next_sparse_ts = now + self._sparse_gap(dev, now)
                self._log("sparse", "Редкие показания «%s» на %d ч (раз в "
                          "30–60 мин)" % (dev.name, hours), did)
            self._persist()
        self.tick()
        return dev

    @staticmethod
    def _amount(v, lo, hi, field, unit):
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(v) or v != int(v) or not lo <= v <= hi:
            raise EmulatorError("%s: целое число %s от %d до %d"
                                % (field, unit, lo, hi))
        return int(v)

    @staticmethod
    def _sparse_gap(dev, now):
        k = zlib.crc32(("%d:%d" % (dev.n, int(now))).encode("utf-8"))
        return (SPARSE_MIN_MIN + k % SPARSE_SPAN_MIN) * 60

    # --- публикация ---------------------------------------------------------------

    def _meta_messages(self, dev):
        base = "/devices/%s" % dev.device_id
        out = [(base + "/meta", json.dumps(
            {"driver": EMU_DRIVER,
             "title": {"en": dev.name, "ru": dev.name}}, ensure_ascii=False)),
               (base + "/meta/name", dev.name)]
        for order, (c, mtype, units) in enumerate(control_specs(), 1):
            m = {"type": mtype, "readonly": True, "order": order}
            if units:
                m["units"] = units
            out.append(("%s/controls/%s/meta" % (base, c), json.dumps(m)))
        return out

    def _value_messages(self, dev, now, numeric=True):
        base = "/devices/%s/controls/" % dev.device_id
        out = [(base + "Uptime", "%d" % max(0, int(now - dev.started_ts))),
               (base + "Serial", dev.serial)]
        if not numeric:
            return out
        p_w = self.power_w(dev, now)
        e = self.energies(dev, now)
        tick = int(now // 5)
        for i, ph in enumerate(PHASES):
            u = NOMINAL_V + 1.5 * _u("U:%d:%d:%d" % (dev.n, i, tick))
            amps = (p_w[i] / (u * POWER_FACTOR)) if p_w[i] > 0 else 0.0
            out += [(base + "P " + ph, "%.1f" % p_w[i]),
                    (base + "Urms " + ph, "%.1f" % u),
                    (base + "Irms " + ph, "%.3f" % amps),
                    (base + "AP energy " + ph, "%.3f" % e[i])]
        out += [(base + "Total P", "%.1f" % sum(p_w)),
                (base + "Total AP energy", "%.3f" % sum(e)),
                (base + "Frequency",
                 "%.2f" % (50.0 + 0.05 * _u("F:%d:%d" % (dev.n, tick))))]
        return out

    def _error_topics(self, dev):
        base = "/devices/%s/controls/" % dev.device_id
        return [base + c + "/meta/error" for c in CONTROL_NAMES]

    def _plan(self, now):
        """Что опубликовать на этом тике: [(topic, payload)]. Меняет состояние
        неисправностей (окончание потери связи и редких показаний)."""
        msgs, dirty = [], False
        for dev in self._sorted():
            if now - dev.anchor_ts >= REANCHOR_S:
                self._reanchor_device(dev, now)
                dirty = True
            msgs += self._meta_messages(dev)
            if dev.loss_until is not None:
                if now < dev.loss_until:
                    msgs += [(t, "r") for t in self._error_topics(dev)]
                    continue
                msgs += [(t, "") for t in self._error_topics(dev)]
                dev.loss_until = None
                dirty = True
                self._log("loss_end", "Связь с «%s» восстановлена" % dev.name,
                          dev.device_id)
            if dev.sparse_until is not None:
                if now >= dev.sparse_until:
                    dev.sparse_until = None
                    dirty = True
                    self._log("sparse_end", "Редкие показания «%s» закончились"
                              % dev.name, dev.device_id)
                else:
                    due = now >= dev.next_sparse_ts
                    msgs += self._value_messages(dev, now, numeric=due)
                    if due:
                        dev.next_sparse_ts = now + self._sparse_gap(dev, now)
                        dirty = True
                    continue
            msgs += self._value_messages(dev, now)
        return msgs, dirty

    def tick(self, now=None):
        """Один шаг публикации (раз в 5 с). `now` — для тестов."""
        with self._lock:
            now = self._clock() if now is None else now
            if self._pending_erase:
                self._erase_pending()
            if not self._enabled or not self._devices:
                return 0
            pub = self._get_publisher()
            if pub is None or not pub.connected:
                if now - self._warned_at > 60:
                    self._warned_at = now
                    log.warning("Эмулятор: нет подключения к MQTT, публикация "
                                "пропущена")
                return 0
            msgs, dirty = self._plan(now)
            sent = 0
            for topic, payload in msgs:
                key = (topic.split("/")[2], topic)
                if self._last.get(key) == payload:
                    continue
                pub.publish(topic, payload, retain=True, qos=1)
                self._last[key] = payload
                sent += 1
            if dirty:
                self._persist()
            self._maybe_probe(now)
            return sent

    # --- проба истории ------------------------------------------------------------

    def _maybe_probe(self, now):
        if self._probe is None or self._history["state"] == "ok":
            return
        if self._enabled_at is None or not self._devices:
            return
        due = max(self._enabled_at + self._probe_delay, self._next_probe_ts)
        if now < due or (self._probe_thread is not None
                         and self._probe_thread.is_alive()):
            return
        self._next_probe_ts = now + self._probe_retry
        self._probe_thread = threading.Thread(
            target=self.probe_history_now, name="emulator-probe", daemon=True)
        self._probe_thread.start()

    def probe_history_now(self):
        """Спросить wb-mqtt-db об истории одного канала эмулятора. Есть точки —
        `ok`; нет — `missing` (предупреждение); ошибка самого запроса — `error`:
        «пусто» и «не спросили» не смешиваются."""
        with self._lock:
            if self._probe is None:
                raise EmulatorError("проба истории не подключена", 409)
            if not self._devices:
                raise EmulatorError("нет приборов: нечего проверять", 409)
            dev = self._sorted()[0]
            ts_from = self._enabled_at or (self._clock() - 3600)
            now = self._clock()
        try:
            count = self._probe(dev.device_id, PROBE_CONTROL, ts_from)
            state, msg = ("ok", "") if count else ("missing", HISTORY_WARNING)
        except Exception as e:
            state, msg = "error", HISTORY_ERROR + str(e)
        with self._lock:
            changed = self._history.get("state") != state
            self._history = {"state": state, "checked_at": int(now),
                             "message": msg}
            if changed and state != "pending":
                self._log("history_" + state,
                          {"ok": "wb-mqtt-db пишет историю эмулятора",
                           "missing": HISTORY_WARNING,
                           "error": msg}[state])
            self._persist()
        return dict(self._history)

    # --- состояние для API ---------------------------------------------------------

    def snapshot(self, journal_limit=50):
        with self._lock:
            now = self._clock()
            devices = []
            for dev in self._sorted():
                p = self.power_w(dev, now)
                e = self.energies(dev, now)
                if dev.loss_until is not None and now < dev.loss_until:
                    state = "loss"
                elif dev.sparse_until is not None and now < dev.sparse_until:
                    state = "sparse"
                else:
                    state = "ok"
                devices.append({
                    "n": dev.n, "device_id": dev.device_id, "name": dev.name,
                    "serial": dev.serial, "kw": dev.kw, "profile": dev.profile,
                    "role": dev.role, "sources": dev.sources, "pct": dev.pct,
                    "power_w": [round(x, 1) for x in p],
                    "power_total_w": round(sum(p), 1),
                    "energy_kwh": [round(x, 3) for x in e],
                    "energy_total_kwh": round(sum(e), 3),
                    "fault": state, "loss_until": dev.loss_until,
                    "sparse_until": dev.sparse_until,
                    "is_emulator": True,
                })
            return {
                "enabled": self._enabled, "enabled_at": self._enabled_at,
                "device_count": len(devices), "devices": devices,
                "history": dict(self._history),
                "journal": list(reversed(self._journal[-journal_limit:])),
                "banner": self.banner_text(),
                "limits": {"max_devices": MAX_DEVICES, "max_kw": MAX_KW},
            }

    def banner_text(self):
        if not self._enabled:
            return ""
        n = len(self._devices)
        return ("Включён эмулятор: %d %s — цифры с ними не настоящие"
                % (n, plural_devices(n)))

    def summary(self):
        """Лёгкая сводка для баннера «Обзора»."""
        with self._lock:
            return {"enabled": self._enabled,
                    "device_count": len(self._devices),
                    "banner": self.banner_text(),
                    "history_state": self._history.get("state"),
                    "history_message": self._history.get("message", "")}

"""Repository: CRUD над БД."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .db import Database

log = logging.getLogger(__name__)

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")
NAME_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f]")
NAME_MAX_LEN = 200
DEVICE_ID_MAX_LEN = 100
VALID_ROLES = ("input", "consumer", "other")


def validate_device_id(s):
    s = (s or "").strip()
    if not s: raise ValueError("device_id не может быть пустым")
    if len(s) > DEVICE_ID_MAX_LEN:
        raise ValueError(f"device_id длиннее {DEVICE_ID_MAX_LEN}")
    if not DEVICE_ID_RE.match(s):
        raise ValueError("device_id: только латиница, цифры, точка, _, -")
    return s


def validate_name(s):
    if s is None: raise ValueError("Имя не может быть None")
    s = str(s).strip()
    if not s: raise ValueError("Имя не может быть пустым")
    if len(s) > NAME_MAX_LEN: raise ValueError(f"Имя длиннее {NAME_MAX_LEN}")
    if NAME_FORBIDDEN_RE.search(s):
        raise ValueError("Имя содержит управляющие символы")
    return s


def validate_role(s):
    s = (s or "").strip().lower()
    if s not in VALID_ROLES:
        raise ValueError(f"role должна быть одной из {VALID_ROLES}")
    return s


@dataclass
class Group:
    id: int
    name: str
    parent_id: Optional[int]
    created_at: int

    @classmethod
    def from_row(cls, row):
        return cls(id=row["id"], name=row["name"],
                   parent_id=row["parent_id"], created_at=row["created_at"])


@dataclass
class Meter:
    id: int
    device_id: str
    display_name: str
    group_id: Optional[int]
    serial_number: Optional[str]
    role: str
    enabled: bool
    notes: Optional[str]
    created_at: int
    updated_at: int
    group_name: Optional[str] = None

    @classmethod
    def from_row(cls, row):
        keys = row.keys()
        return cls(
            id=row["id"], device_id=row["device_id"],
            display_name=row["display_name"], group_id=row["group_id"],
            serial_number=row["serial_number"], role=row["role"],
            enabled=bool(row["enabled"]), notes=row["notes"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            group_name=row["group_name"] if "group_name" in keys else None,
        )

    def to_dict(self):
        return {
            "id": self.id, "device_id": self.device_id,
            "display_name": self.display_name, "group_id": self.group_id,
            "group_name": self.group_name, "serial_number": self.serial_number,
            "role": self.role, "enabled": self.enabled, "notes": self.notes,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


class GroupRepo:
    def __init__(self, db): self._db = db

    def get_by_name(self, name):
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM meter_groups WHERE name = ? COLLATE NOCASE",
                (name,)).fetchone()
            return Group.from_row(row) if row else None

    def get_by_id(self, gid):
        with self._db.read() as c:
            row = c.execute("SELECT * FROM meter_groups WHERE id = ?",
                            (gid,)).fetchone()
            return Group.from_row(row) if row else None

    def list_all(self):
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM meter_groups ORDER BY name COLLATE NOCASE"
            ).fetchall()
            return [Group.from_row(r) for r in rows]

    def create(self, name):
        name = validate_name(name)
        now = int(time.time())
        with self._db.transaction() as c:
            try:
                cur = c.execute(
                    "INSERT INTO meter_groups (name, parent_id, created_at) "
                    "VALUES (?, NULL, ?)", (name, now))
            except Exception as e:
                raise ValueError(f"Не удалось создать группу: {e}")
            gid = cur.lastrowid
        log.info("Создана группа: %s (id=%d)", name, gid)
        return Group(id=gid, name=name, parent_id=None, created_at=now)

    def get_or_create(self, name):
        existing = self.get_by_name(name)
        if existing: return existing
        return self.create(name)

    def delete(self, gid):
        with self._db.transaction() as c:
            cur = c.execute("DELETE FROM meter_groups WHERE id = ?", (gid,))
            return cur.rowcount > 0


_METER_BASE_SELECT = """
SELECT m.*, g.name AS group_name
FROM meters m
LEFT JOIN meter_groups g ON g.id = m.group_id
"""


class MeterRepo:
    def __init__(self, db, groups):
        self._db = db
        self._groups = groups

    def get_by_id(self, mid):
        with self._db.read() as c:
            row = c.execute(_METER_BASE_SELECT + " WHERE m.id = ?",
                            (mid,)).fetchone()
            return Meter.from_row(row) if row else None

    def get_by_device_id(self, device_id):
        with self._db.read() as c:
            row = c.execute(_METER_BASE_SELECT + " WHERE m.device_id = ?",
                            (device_id,)).fetchone()
            return Meter.from_row(row) if row else None

    def list_all(self, only_enabled=False):
        sql = _METER_BASE_SELECT
        if only_enabled:
            sql += " WHERE m.enabled = 1"
        sql += " ORDER BY m.display_name COLLATE NOCASE"
        with self._db.read() as c:
            rows = c.execute(sql).fetchall()
            return [Meter.from_row(r) for r in rows]

    def count(self):
        with self._db.read() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM meters").fetchone()
            return int(row["n"])

    def add(self, device_id, display_name, group=None,
            role="consumer", notes=None):
        device_id = validate_device_id(device_id)
        display_name = validate_name(display_name)
        role = validate_role(role)
        if notes is not None:
            notes = str(notes).strip()
            if NAME_FORBIDDEN_RE.search(notes):
                raise ValueError("notes содержат управляющие символы")
            if len(notes) > 1000:
                raise ValueError("notes длиннее 1000 символов")
        group_id = None
        if group:
            group_id = self._groups.get_or_create(group).id
        now = int(time.time())
        with self._db.transaction() as c:
            existing = c.execute(
                "SELECT id FROM meters WHERE device_id = ?",
                (device_id,)).fetchone()
            if existing:
                raise ValueError(
                    f"Счётчик с device_id={device_id!r} уже есть")
            cur = c.execute(
                "INSERT INTO meters (device_id, display_name, group_id, "
                "role, enabled, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (device_id, display_name, group_id, role, notes, now, now))
            mid = cur.lastrowid
        log.info("Добавлен счётчик: %s -> %r (id=%d, group=%s)",
                 device_id, display_name, mid, group)
        return self.get_by_id(mid)

    def update(self, device_id, *, display_name=None, group=None,
               role=None, enabled=None, notes=None, serial_number=None):
        device_id = validate_device_id(device_id)
        existing = self.get_by_device_id(device_id)
        if not existing:
            raise ValueError(f"Счётчик с device_id={device_id!r} не найден")
        sets = []
        params = []
        if display_name is not None:
            sets.append("display_name = ?")
            params.append(validate_name(display_name))
        if group is not None:
            if group == "":
                sets.append("group_id = NULL")
            else:
                gid = self._groups.get_or_create(group).id
                sets.append("group_id = ?"); params.append(gid)
        if role is not None:
            sets.append("role = ?"); params.append(validate_role(role))
        if enabled is not None:
            sets.append("enabled = ?"); params.append(1 if enabled else 0)
        if notes is not None:
            notes = str(notes).strip()
            if NAME_FORBIDDEN_RE.search(notes):
                raise ValueError("notes содержат управляющие символы")
            if len(notes) > 1000:
                raise ValueError("notes длиннее 1000 символов")
            sets.append("notes = ?"); params.append(notes or None)
        if serial_number is not None:
            serial_number = str(serial_number).strip() or None
            sets.append("serial_number = ?"); params.append(serial_number)
        if not sets: return existing
        sets.append("updated_at = ?"); params.append(int(time.time()))
        params.append(device_id)
        with self._db.transaction() as c:
            c.execute(
                f"UPDATE meters SET {', '.join(sets)} WHERE device_id = ?",
                tuple(params))
        return self.get_by_device_id(device_id)

    def remove(self, device_id):
        device_id = validate_device_id(device_id)
        with self._db.transaction() as c:
            cur = c.execute("DELETE FROM meters WHERE device_id = ?",
                            (device_id,))
            removed = cur.rowcount > 0
        if removed: log.info("Удалён счётчик: %s", device_id)
        return removed

    def update_serial_observed(self, device_id, serial):
        if not serial: return
        with self._db.transaction() as c:
            row = c.execute(
                "SELECT serial_number FROM meters WHERE device_id = ?",
                (device_id,)).fetchone()
            if not row: return
            current = row["serial_number"]
            if current == serial: return
            c.execute(
                "UPDATE meters SET serial_number = ?, updated_at = ? "
                "WHERE device_id = ?",
                (serial, int(time.time()), device_id))
        log.info("Обновлён серийник для %s: %r -> %r",
                 device_id, current, serial)


class KvRepo:
    def __init__(self, db): self._db = db

    def get(self, key, default=None):
        with self._db.read() as c:
            row = c.execute("SELECT value FROM kv WHERE key = ?",
                            (key,)).fetchone()
            if not row: return default
            try: return json.loads(row["value"])
            except (TypeError, json.JSONDecodeError): return row["value"]

    def set(self, key, value):
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        now = int(time.time())
        with self._db.transaction() as c:
            c.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, now))

    def delete(self, key):
        with self._db.transaction() as c:
            c.execute("DELETE FROM kv WHERE key = ?", (key,))


def import_registry_from_config(meters_repo, kv, config_meters):
    if kv.get("registry_imported_from_config"): return 0
    if meters_repo.count() > 0:
        kv.set("registry_imported_from_config", True); return 0
    if not config_meters:
        kv.set("registry_imported_from_config", True); return 0
    added = 0
    for m in config_meters:
        try:
            meters_repo.add(device_id=m.device_id,
                            display_name=m.display_name, group=m.group)
            added += 1
        except ValueError as e:
            log.warning("Не смог импортировать %s: %s", m.device_id, e)
    kv.set("registry_imported_from_config", True)
    log.info("Импортировано из YAML в БД: %d счётчиков", added)
    return added

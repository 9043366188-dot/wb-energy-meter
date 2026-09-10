"""Фикстура «старой» БД v0.11.1 для воспроизводимых сценариев этапа A ТЗ
(docs/TZ-metering-architecture-dashboard.md, §12).

Строит временную SQLite БД через реальные миграции 001-004 и реальные
репозитории (`Database`/`GroupRepo`/`MeterRepo` из `wb_energy_meter`),
заполняет сценарием §5.2 ТЗ: ввод ГРЩ-1=100 кВт·ч, Цех=60 (внутри
которого физически Станок=20), Серверная=30 — ровно та комбинация, на
которой текущий плоский input/consumer расчёт (`api.py::api_reports_balance`)
даёт двойной счёт, если «Станок» тоже помечен `role=consumer`: ничто в
схеме 0.11.1 не выражает, что его расход уже входит в измерение «Цех»
(`meter_groups.parent_id` в БД есть, но `GroupRepo.create` создаёт
плоскую группу — см. наблюдение ТЗ §3, вторая строка таблицы).
"""

from __future__ import annotations

import os
import shutil
import tempfile

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.wb_db_client import HistoryPoint


class LegacyDbFixture:
    def __init__(self, tmpdir, db, groups_repo, meters_repo, meters):
        self.tmpdir = tmpdir
        self.db = db
        self.groups_repo = groups_repo
        self.meters_repo = meters_repo
        self.meters = meters  # dict: ключ сценария -> Meter

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def build_legacy_db(prefix="wbem_test_legacy_"):
    """Сценарий §5.2 ТЗ: ГРЩ-1(вход)=100, Цех=60 (включает Станок=20),
    Серверная=30. Возвращает `LegacyDbFixture` с реальными repo/БД —
    вызывающий код обязан вызвать `.cleanup()`."""
    tmpdir = tempfile.mkdtemp(prefix=prefix)
    db = Database(path=os.path.join(tmpdir, "state.db"))
    db.open()
    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)

    meters = {}
    meters["input"] = meters_repo.add(
        "input.grsh1", "Ввод ГРЩ-1", group=None, role="input")
    meters["tsex"] = meters_repo.add(
        "cons.tsex", "Цех", group="Цех", role="consumer")
    meters["server"] = meters_repo.add(
        "cons.server", "Серверная", group="Серверная", role="consumer")
    # Станок физически внутри линии «Цех» — его расход уже входит в
    # измерение «Цех». В плоской модели 0.11.1 это никак не выражено:
    # ничто не мешает пометить его тоже role=consumer.
    meters["stanok"] = meters_repo.add(
        "cons.stanok", "Станок", group="Цех", role="consumer")

    # Отключённый прибор — должен архивироваться с сохранением истории,
    # а не пропадать (ТЗ §6.2 «Ограничения»: «Существующие использованные
    # точки/приборы архивируются, а не удаляются»).
    meters["retired"] = meters_repo.add(
        "cons.retired", "Списанный счётчик", group="Серверная", role="consumer")
    meters_repo.update("cons.retired", enabled=False)

    return LegacyDbFixture(tmpdir, db, groups_repo, meters_repo, meters)


# Энергии сценария §5.2 ТЗ (кВт·ч за тестовый период). Совпадают с
# числовым примером самого ТЗ: «ввод 100 кВт·ч; цех 60; серверная 30;
# внутри цеха станок 20».
SCENARIO_ENERGY_KWH = {
    "input": 100.0,
    "tsex": 60.0,    # включает stanok
    "server": 30.0,
    "stanok": 20.0,  # подмножество tsex, НЕ независимое измерение
}


def reset_history_points(t0=1_757_400_000):
    """A13 ТЗ: накопитель `100 -> 0 -> 150` — внутренний сброс/замена
    между известными точками, который проверка только по границам
    периода (`end - start`) не обнаруживает."""
    return [
        HistoryPoint(timestamp=t0, value=100.0),
        HistoryPoint(timestamp=t0 + 1800, value=0.0),   # сброс/замена
        HistoryPoint(timestamp=t0 + 3600, value=150.0),
    ]

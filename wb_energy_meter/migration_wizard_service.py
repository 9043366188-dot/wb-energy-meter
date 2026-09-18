"""Партия 3, задача 2: мастер переноса связей старой модели планов
(`plan_links`) в электрическую топологию v2 (`electrical_nodes`/
`electrical_edges`) — docs/TZ-batch3-structure-inspector-legacy.md §2
(строка про §31.5 большого ТЗ).

Большое ТЗ прямо запрещает автоматический перенос: «Только после
подтверждения смысла каждой связи. Из рисунка между группами нельзя
достоверно вывести питающую линию» — поэтому здесь нет никакой
эвристики «угадывания» узла по зоне/группе; для каждой связи
пользователь явно указывает (через API — какой узел выбрать, решает
интерфейс), чем является каждый конец: уже существующий узел
(`node_id`), уже перенесённая ранее зона (`legacy_zone_id` — сверяется с
`migration_map`) или новый узел (`new_node`).

Идемпотентность (повторный запуск не плодит дубли — сверяться с
`migration_map`, он для этого и заведён, см. TZ §2):

- каждая перенесённая связь `plan_links.id -> electrical_edges.id`
  записывается в `migration_map`; повторное подтверждение той же связи
  возвращает уже существующий `edge_id` (`status="already_migrated"`),
  вторую запись не создаёт;
- то же для зоны, если она сопоставлена узлу через `legacy_zone_id` —
  `plan_zones.id -> electrical_nodes.id`; следующая связь, ссылающаяся
  на ту же зону тем же способом, попадает на тот же узел, а не плодит
  дубль. Работает и внутри одной пачки подтверждений (общая транзакция:
  чтение видит собственную ещё не зафиксированную запись).

Создаёт только ЧЕРНОВИКИ рёбер (`ElectricalEdgeRepo.add_draft`) —
проверка леса и публикация НЕ дублируются здесь: это уже делают
существующие `POST /api/v2/topology/validate` и `.../topology/publish`
(там же лежит вся логика проверки радиальности/циклов). Вся пачка
подтверждений — одна атомарная операция (проверяется и применяется в
одной транзакции с ревизией, как и остальные предметные записи партии
2/3): если в пачке есть ошибка — не создаётся НИ ОДНА запись."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .legacy_migration import SCHEMA_MIGRATION_VERSION


class WizardError(ValueError):
    """Ошибка в одном из подтверждений пачки — 400, ничего не
    сохраняется (вся пачка атомарна, см. модульный docstring)."""


@dataclass
class ConfirmationResult:
    plan_link_id: int
    status: str  # "created" | "already_migrated"
    edge_id: int
    from_node_id: Optional[int]
    to_node_id: Optional[int]

    def to_dict(self):
        return {
            "plan_link_id": self.plan_link_id, "status": self.status,
            "edge_id": self.edge_id, "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
        }


def _map_get(c, legacy_table, legacy_id, new_table) -> Optional[int]:
    row = c.execute(
        "SELECT new_id FROM migration_map WHERE legacy_table = ? AND "
        "legacy_id = ? AND new_table = ?",
        (legacy_table, legacy_id, new_table)).fetchone()
    return row["new_id"] if row else None


def _map_put(c, legacy_table, legacy_id, new_table, new_id, version) -> None:
    now = int(time.time())
    c.execute(
        "INSERT INTO migration_map (legacy_table, legacy_id, new_table, "
        "new_id, migration_version, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (legacy_table, legacy_id, new_table, new_id, version, now))


def _resolve_node_ref(c, node_repo, ref: Any, end_label: str) -> int:
    """Определить id узла для одного конца связи. `ref` — один из:
    {"node_id": N} — уже существующий узел;
    {"legacy_zone_id": Z} — искать в migration_map; если для Z ещё нет
        сопоставления, требуется ТАКЖЕ node_id или new_node в этом же
        вызове (сопоставление создаётся здесь же);
    {"new_node": {"code":.., "name":.., "kind":.., "location_id":..}} —
        завести новый узел.
    Комбинация legacy_zone_id + new_node — обычный случай первого
    переноса зоны: узел заводится и сразу запоминается за зоной."""
    if not isinstance(ref, dict):
        raise WizardError(
            f"{end_label}: требуется объект с node_id, legacy_zone_id и/или "
            f"new_node")

    legacy_zone_id = ref.get("legacy_zone_id")
    if legacy_zone_id is not None:
        mapped = _map_get(c, "plan_zones", legacy_zone_id, "electrical_nodes")
        if mapped is not None:
            return mapped

    node_id = ref.get("node_id")
    if node_id is not None:
        if node_repo.get_by_id(node_id) is None:
            raise WizardError(f"{end_label}: узел {node_id} не найден")
    elif isinstance(ref.get("new_node"), dict):
        nn = ref["new_node"]
        node = node_repo.add(nn.get("code"), nn.get("name"), nn.get("kind"),
                              location_id=nn.get("location_id"))
        node_id = node.id
    else:
        raise WizardError(
            f"{end_label}: узел не определён — укажите node_id, new_node, "
            f"либо legacy_zone_id уже перенесённой ранее зоны")

    if legacy_zone_id is not None:
        _map_put(c, "plan_zones", legacy_zone_id, "electrical_nodes", node_id,
                  SCHEMA_MIGRATION_VERSION)
    return node_id


def confirm_links(c, plan_link_repo, node_repo, edge_repo,
                   confirmations: List[Dict[str, Any]]) -> List[ConfirmationResult]:
    """Обработать пачку подтверждений внутри уже открытой транзакции
    `c` (вызывающий код — api_v2.py — оборачивает это в
    `with_revision_check`). Бросает WizardError на первой же проблеме —
    вызывающий код откатывает транзакцию целиком (см. модульный
    docstring: пачка атомарна)."""
    if not isinstance(confirmations, list) or not confirmations:
        raise WizardError("confirmations должен быть непустым списком")

    results: List[ConfirmationResult] = []
    for conf in confirmations:
        if not isinstance(conf, dict):
            raise WizardError("каждое подтверждение должно быть объектом")
        link_id = conf.get("plan_link_id")
        if link_id is None:
            raise WizardError("plan_link_id обязателен для каждого подтверждения")
        link = plan_link_repo.get_by_id(link_id)
        if link is None:
            raise WizardError(f"plan_link {link_id} не найден")

        existing_edge_id = _map_get(c, "plan_links", link_id, "electrical_edges")
        if existing_edge_id is not None:
            edge = edge_repo.get_by_id(existing_edge_id)
            results.append(ConfirmationResult(
                plan_link_id=link_id, status="already_migrated",
                edge_id=existing_edge_id,
                from_node_id=edge.from_node_id if edge else None,
                to_node_id=edge.to_node_id if edge else None))
            continue

        from_node_id = _resolve_node_ref(
            c, node_repo, conf.get("from_node"), f"связь {link_id}, конец «от»")
        to_node_id = _resolve_node_ref(
            c, node_repo, conf.get("to_node"), f"связь {link_id}, конец «к»")

        edge_fields = conf.get("edge") or {}
        if not isinstance(edge_fields, dict):
            raise WizardError(f"связь {link_id}: edge должен быть объектом")
        rated_current_a = edge_fields.get("rated_current_a", link.rated_current_a)
        edge = edge_repo.add_draft(
            from_node_id, to_node_id,
            code=edge_fields.get("code"),
            name=edge_fields.get("name") or link.label,
            primary_point_id=conf.get("primary_point_id"),
            phase_count=edge_fields.get("phase_count"),
            rated_current_a=rated_current_a,
            cable_note=edge_fields.get("cable_note"),
        )
        _map_put(c, "plan_links", link_id, "electrical_edges", edge.id,
                 SCHEMA_MIGRATION_VERSION)

        results.append(ConfirmationResult(
            plan_link_id=link_id, status="created", edge_id=edge.id,
            from_node_id=from_node_id, to_node_id=to_node_id))
    return results

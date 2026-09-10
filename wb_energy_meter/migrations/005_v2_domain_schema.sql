-- Migration 005: доменная схема v2 (ТЗ docs/TZ-metering-architecture-dashboard.md)
--
-- Вводит постоянную точку учёта, отделённую от физического прибора, дерево
-- мест, электрическую сеть (лес радиальных деревьев), учётные группы с
-- версионируемой иерархией, границы баланса, универсальные объекты плана
-- на двух типах холста и служебные таблицы ревизий/миграции.
--
-- ВАЖНО: этот файл применяется НЕ через обычный Database._apply_migrations
-- (executescript без транзакции), а через _apply_migration_atomic()
-- (db.py) — BEGIN/COMMIT/ROLLBACK держит вызывающий код, поэтому здесь
-- нет собственных BEGIN/COMMIT (см. docs/migration-plan-v2.md §4).
--
-- Ничего не переносит из старых meters/meter_groups/site_plans — только
-- создаёт новые таблицы. Перенос данных — отдельная DML-часть той же
-- миграции (repo/скрипт), не эта DDL-часть.
--
-- Денормализованные "текущие" колонки (locations.parent_id,
-- metering_points.enabled, meter_groups.parent_id) — это КЭШ, который
-- пишут только сервисы точек/мест/групп ОДНОВРЕМЕННО с соответствующей
-- строкой версии (location_parent_bindings/point_state_versions/
-- group_parent_bindings) в одной транзакции. API никогда не пишет их
-- напрямую отдельным PATCH — это и есть смысл "read-only проекции"
-- из ТЗ §4.1: проекция читается быстро одним JOIN, но её пишет только
-- versioning-сервис, а не произвольный вызывающий код.

-- === Ревизии, лог изменений, карта миграции (создаются первыми — на них
-- === ссылаются revision_id многих таблиц ниже) ===

CREATE TABLE configuration_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_json   TEXT,           -- метаданные состояния на момент ревизии
    schema_version  INTEGER NOT NULL,
    content_hash    TEXT,
    created_at      INTEGER NOT NULL
);

CREATE TABLE change_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type   TEXT    NOT NULL,   -- 'metering_point'|'location'|'group'|...
    entity_id     INTEGER NOT NULL,
    action        TEXT    NOT NULL,   -- 'create'|'update'|'archive'|'replace_meter'|...
    old_value     TEXT,               -- JSON
    new_value     TEXT,               -- JSON
    reason        TEXT,
    revision_id   INTEGER REFERENCES configuration_revisions(id),
    effective_from INTEGER NOT NULL,
    recorded_at   INTEGER NOT NULL
);
CREATE INDEX idx_change_log_entity ON change_log(entity_type, entity_id);

CREATE TABLE migration_map (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_table      TEXT    NOT NULL,
    legacy_id         INTEGER NOT NULL,
    new_table         TEXT    NOT NULL,
    new_id            INTEGER NOT NULL,
    migration_version INTEGER NOT NULL,
    created_at        INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_migration_map_legacy
    ON migration_map(legacy_table, legacy_id, new_table);

-- === Дерево мест (ТЗ §4.2) ===

CREATE TABLE locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   INTEGER REFERENCES locations(id) ON DELETE RESTRICT,  -- кэш, см. шапку файла
    kind        TEXT    NOT NULL,   -- object|building|floor|room|zone|installation_point
    name        TEXT    NOT NULL,
    name_norm   TEXT    NOT NULL,
    code        TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX idx_locations_parent ON locations(parent_id);
CREATE UNIQUE INDEX idx_locations_code ON locations(code) WHERE code IS NOT NULL;
CREATE INDEX idx_locations_name_norm ON locations(name_norm);

CREATE TABLE location_parent_bindings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES locations(id) ON DELETE RESTRICT,  -- NULL = корень
    valid_from  INTEGER NOT NULL,
    valid_to    INTEGER,   -- NULL = открытый (текущий) интервал
    revision_id INTEGER REFERENCES configuration_revisions(id),
    created_at  INTEGER NOT NULL
);
CREATE INDEX idx_location_parent_bindings_loc
    ON location_parent_bindings(location_id, valid_from, valid_to);
-- не более одного открытого интервала на место (§6.1: "в пределах каждой
-- сущности/роли интервалы не пересекаются")
CREATE UNIQUE INDEX idx_location_parent_bindings_open
    ON location_parent_bindings(location_id) WHERE valid_to IS NULL;

-- === Прибор, поток измерений, точка учёта (ТЗ §4.1) ===

CREATE TABLE meter_sources (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id       INTEGER NOT NULL REFERENCES meters(id) ON DELETE RESTRICT,
    controller_key TEXT    NOT NULL,
    device_id      TEXT    NOT NULL,
    valid_from     INTEGER NOT NULL,
    valid_to       INTEGER,
    created_at     INTEGER NOT NULL
);
CREATE INDEX idx_meter_sources_meter ON meter_sources(meter_id, valid_from, valid_to);
-- одна MQTT-адресация в данный момент относится к одному физическому
-- прибору (§4.1: "уникальность текущей пары (controller_key,device_id)
-- обеспечить на meter_sources")
CREATE UNIQUE INDEX idx_meter_sources_open_addr
    ON meter_sources(controller_key, device_id) WHERE valid_to IS NULL;

CREATE TABLE metering_points (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    code                   TEXT    NOT NULL UNIQUE,
    name                   TEXT    NOT NULL,
    description            TEXT,
    installation_location_id INTEGER REFERENCES locations(id),  -- кэш, см. шапку
    installation_note      TEXT,
    enabled                INTEGER NOT NULL DEFAULT 1,          -- кэш, см. шапку
    archived_at            INTEGER,
    created_at             INTEGER NOT NULL,
    updated_at             INTEGER NOT NULL
);
CREATE INDEX idx_metering_points_location ON metering_points(installation_location_id);
CREATE INDEX idx_metering_points_enabled ON metering_points(enabled);

CREATE TABLE point_state_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id    INTEGER NOT NULL REFERENCES metering_points(id) ON DELETE CASCADE,
    enabled     INTEGER NOT NULL,   -- 0/1 — состав активных точек на интервал
    valid_from  INTEGER NOT NULL,
    valid_to    INTEGER,
    revision_id INTEGER REFERENCES configuration_revisions(id),
    created_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_point_state_versions_open
    ON point_state_versions(point_id) WHERE valid_to IS NULL;

-- point_bindings: непересечение primary-привязок/профилей — не выражается
-- одним SQL CHECK/UNIQUE (total_3p пересекается со всеми тремя фазами,
-- §4.1) — проверяется сервисом (point_repo.py) в транзакции.
CREATE TABLE point_bindings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id          INTEGER NOT NULL REFERENCES metering_points(id) ON DELETE CASCADE,
    meter_source_id   INTEGER NOT NULL REFERENCES meter_sources(id) ON DELETE RESTRICT,
    channel_profile   TEXT    NOT NULL,  -- total_3p|phase_l1|phase_l2|phase_l3
    role              TEXT    NOT NULL DEFAULT 'primary',  -- primary|check
    valid_from        INTEGER NOT NULL,
    valid_to          INTEGER,
    replacement_note  TEXT,
    created_at        INTEGER NOT NULL
);
CREATE INDEX idx_point_bindings_point
    ON point_bindings(point_id, valid_from, valid_to);
CREATE INDEX idx_point_bindings_source
    ON point_bindings(meter_source_id, valid_from, valid_to);

CREATE TABLE point_locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id    INTEGER NOT NULL REFERENCES metering_points(id) ON DELETE CASCADE,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE RESTRICT,
    valid_from  INTEGER NOT NULL,
    valid_to    INTEGER,
    created_at  INTEGER NOT NULL
);
-- место установки — максимум одно на момент времени (§4.2)
CREATE UNIQUE INDEX idx_point_locations_open
    ON point_locations(point_id) WHERE valid_to IS NULL;

CREATE TABLE point_served_locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id    INTEGER NOT NULL REFERENCES metering_points(id) ON DELETE CASCADE,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE RESTRICT,
    valid_from  INTEGER NOT NULL,
    valid_to    INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX idx_point_served_locations_point
    ON point_served_locations(point_id, valid_from, valid_to);

-- === Учётные группы: используем существующие meter_groups/parent_id,
-- === не заводим вторую сущность (ТЗ §2 таблица решений) ===

ALTER TABLE meter_groups ADD COLUMN category TEXT;

CREATE TABLE group_parent_bindings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL REFERENCES meter_groups(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES meter_groups(id) ON DELETE RESTRICT,
    valid_from  INTEGER NOT NULL,
    valid_to    INTEGER,
    revision_id INTEGER REFERENCES configuration_revisions(id),
    created_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_group_parent_bindings_open
    ON group_parent_bindings(group_id) WHERE valid_to IS NULL;

-- v2 группы содержат ТОЧКИ (metering_points), а не приборы (§4.4) —
-- существующая meters.group_id остаётся legacy-полем для старого UI.
CREATE TABLE group_memberships (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL REFERENCES meter_groups(id) ON DELETE CASCADE,
    point_id    INTEGER NOT NULL REFERENCES metering_points(id) ON DELETE CASCADE,
    valid_from  INTEGER NOT NULL,
    valid_to    INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX idx_group_memberships_point ON group_memberships(point_id, valid_from, valid_to);
CREATE INDEX idx_group_memberships_group ON group_memberships(group_id, valid_from, valid_to);
-- уникальная активная связь точка-группа (одна и та же пара не может быть
-- добавлена дважды одновременно; повторное включение той же точки в ту
-- же группу в разное время — разные строки, это нормально)
CREATE UNIQUE INDEX idx_group_memberships_open
    ON group_memberships(group_id, point_id) WHERE valid_to IS NULL;

-- === Электрическая сеть: лес направленных радиальных деревьев (ТЗ §4.3) ===

CREATE TABLE electrical_nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,   -- source|panel|junction|load
    location_id INTEGER REFERENCES locations(id),
    archived_at INTEGER,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX idx_electrical_nodes_location ON electrical_nodes(location_id);

-- edge — единственная электрическая привязка точки (§4.3: "точка не
-- является node"). state=draft — черновик, не входит в автоматический
-- баланс, пока не опубликован через POST /api/v2/topology/publish.
-- valid_from/valid_to — фактический период действия ОПУБЛИКОВАННОЙ связи
-- (при повторной публикации со сменой родителя старая связь закрывается,
-- новая открывается — так строится as_was-история сети).
CREATE TABLE electrical_edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT,
    name            TEXT,
    from_node_id    INTEGER NOT NULL REFERENCES electrical_nodes(id) ON DELETE RESTRICT,
    to_node_id      INTEGER NOT NULL REFERENCES electrical_nodes(id) ON DELETE RESTRICT,
    primary_point_id INTEGER REFERENCES metering_points(id),
    phase_count     INTEGER,
    rated_current_a REAL,
    cable_note      TEXT,
    state           TEXT    NOT NULL DEFAULT 'draft',  -- draft|published
    valid_from      INTEGER,
    valid_to        INTEGER,
    revision_id     INTEGER REFERENCES configuration_revisions(id),
    archived_at     INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    CHECK (from_node_id != to_node_id)
);
CREATE INDEX idx_electrical_edges_from ON electrical_edges(from_node_id, valid_from, valid_to);
CREATE INDEX idx_electrical_edges_to ON electrical_edges(to_node_id, valid_from, valid_to);
CREATE INDEX idx_electrical_edges_point ON electrical_edges(primary_point_id);
-- узел принимает максимум одну ДЕЙСТВУЮЩУЮ опубликованную питающую связь
-- (§4.3: "у узла максимум одна действующая питающая связь")
CREATE UNIQUE INDEX idx_electrical_edges_one_parent
    ON electrical_edges(to_node_id)
    WHERE state = 'published' AND valid_to IS NULL;
-- точка не может одновременно измерять две опубликованные связи (§4.3:
-- "одна точка в данный момент измеряет максимум одну основную
-- электрическую связь")
CREATE UNIQUE INDEX idx_electrical_edges_one_point
    ON electrical_edges(primary_point_id)
    WHERE state = 'published' AND valid_to IS NULL AND primary_point_id IS NOT NULL;

-- === Границы баланса (ТЗ §5.2) ===

CREATE TABLE balance_scopes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT,
    archived_at INTEGER,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE balance_members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id    INTEGER NOT NULL REFERENCES balance_scopes(id) ON DELETE CASCADE,
    point_id    INTEGER NOT NULL REFERENCES metering_points(id) ON DELETE RESTRICT,
    side        TEXT    NOT NULL,   -- input|output
    valid_from  INTEGER NOT NULL,
    valid_to    INTEGER,
    created_at  INTEGER NOT NULL,
    CHECK (side IN ('input', 'output'))
);
CREATE INDEX idx_balance_members_scope ON balance_members(scope_id, valid_from, valid_to);
-- "измерение не может стоять с обеих сторон" — точка не может иметь два
-- открытых членства (input и output) в одной границе одновременно
CREATE UNIQUE INDEX idx_balance_members_open
    ON balance_members(scope_id, point_id) WHERE valid_to IS NULL;

-- === План и однолинейная схема: рёбра site_plans расширяются, а не
-- === дублируются — пересобираем таблицу, чтобы снять NOT NULL с
-- === image_file (у single_line-плана фон необязателен, §7.1) ===

CREATE TABLE site_plans_v2 (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    plan_kind     TEXT    NOT NULL DEFAULT 'floor',  -- floor|single_line
    image_file    TEXT,        -- NULL допустим только для single_line
    image_width   INTEGER,
    image_height  INTEGER,
    canvas_width  INTEGER,     -- для single_line без изображения
    canvas_height INTEGER,
    canvas_revision INTEGER NOT NULL DEFAULT 1,
    is_default    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    CHECK (plan_kind IN ('floor', 'single_line')),
    CHECK (plan_kind = 'single_line' OR image_file IS NOT NULL)
);
INSERT INTO site_plans_v2
    (id, name, plan_kind, image_file, image_width, image_height,
     canvas_width, canvas_height, canvas_revision, is_default,
     created_at, updated_at)
SELECT id, name, 'floor', image_file, image_width, image_height,
       image_width, image_height, 1, is_default, created_at, updated_at
FROM site_plans;
DROP TABLE site_plans;
ALTER TABLE site_plans_v2 RENAME TO site_plans;
-- plan_zones/plan_links ссылались на site_plans(id) без ON UPDATE —
-- id-шники сохранены 1:1 при пересборке, старые FK остаются валидными.

CREATE TABLE plan_image_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id      INTEGER NOT NULL REFERENCES site_plans(id) ON DELETE CASCADE,
    image_file   TEXT    NOT NULL,
    image_width  INTEGER NOT NULL,
    image_height INTEGER NOT NULL,
    file_hash    TEXT,
    created_at   INTEGER NOT NULL
);
CREATE INDEX idx_plan_image_versions_plan ON plan_image_versions(plan_id, created_at);

-- ровно одна из point_id/location_id/group_id/node_id — для kind в
-- point|location|group|node; аннотация (kind='annotation') и порт
-- (kind='port', node обязателен) — см. CHECK ниже (§7.1: "аннотация
-- допускает ноль ссылок").
CREATE TABLE plan_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id          INTEGER NOT NULL REFERENCES site_plans(id) ON DELETE CASCADE,
    kind             TEXT    NOT NULL,  -- point|location|group|node|port|annotation
    point_id         INTEGER REFERENCES metering_points(id) ON DELETE CASCADE,
    location_id      INTEGER REFERENCES locations(id) ON DELETE CASCADE,
    group_id         INTEGER REFERENCES meter_groups(id) ON DELETE CASCADE,
    node_id          INTEGER REFERENCES electrical_nodes(id) ON DELETE CASCADE,
    target_plan_id   INTEGER REFERENCES site_plans(id),  -- для kind='port'
    geometry         TEXT    NOT NULL,   -- JSON: {x,y} | [[x,y],...]
    coord_space      TEXT    NOT NULL,   -- image_px_xy_v2|canvas_xy_v2|legacy_leaflet_yx_v1
    image_version_id INTEGER REFERENCES plan_image_versions(id),
    label            TEXT,
    sort_order       INTEGER NOT NULL DEFAULT 0,
    revision_id      INTEGER REFERENCES configuration_revisions(id),
    archived_at      INTEGER,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    CHECK (
        (CASE WHEN point_id IS NOT NULL THEN 1 ELSE 0 END
       + CASE WHEN location_id IS NOT NULL THEN 1 ELSE 0 END
       + CASE WHEN group_id IS NOT NULL THEN 1 ELSE 0 END
       + CASE WHEN node_id IS NOT NULL THEN 1 ELSE 0 END) <= 1
    )
);
CREATE INDEX idx_plan_items_plan ON plan_items(plan_id);
CREATE INDEX idx_plan_items_point ON plan_items(point_id);
CREATE INDEX idx_plan_items_location ON plan_items(location_id);
CREATE INDEX idx_plan_items_group ON plan_items(group_id);
CREATE INDEX idx_plan_items_node ON plan_items(node_id);

CREATE TABLE plan_edge_views (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER NOT NULL REFERENCES site_plans(id) ON DELETE CASCADE,
    edge_id       INTEGER NOT NULL REFERENCES electrical_edges(id) ON DELETE CASCADE,
    from_item_id  INTEGER REFERENCES plan_items(id) ON DELETE SET NULL,
    to_item_id    INTEGER REFERENCES plan_items(id) ON DELETE SET NULL,
    waypoints     TEXT,      -- JSON [[x,y],...]
    view_kind     TEXT    NOT NULL DEFAULT 'structural',  -- structural|cable_route
    confirmed_at  INTEGER,   -- для cable_route — явное подтверждение (§7.1)
    revision_id   INTEGER REFERENCES configuration_revisions(id),
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    CHECK (view_kind IN ('structural', 'cable_route'))
);
CREATE INDEX idx_plan_edge_views_plan ON plan_edge_views(plan_id);
CREATE INDEX idx_plan_edge_views_edge ON plan_edge_views(edge_id);

INSERT INTO schema_migrations (version, name, applied_at)
VALUES (5, 'v2_domain_schema', strftime('%s','now'));

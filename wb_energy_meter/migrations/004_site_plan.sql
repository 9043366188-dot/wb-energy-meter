-- Migration 004: план объекта — зоны на схеме и кабельные связи (ТЗ v0.11.0)
--
-- Зона на плане ссылается на СУЩЕСТВУЮЩУЮ meter_groups — вторая сущность
-- "зона" не заводится. Координаты хранятся в пикселях исходного
-- изображения, ось Y — вниз от левого верхнего угла; в geometry/anchor/
-- waypoints порядок [y, x] (по аналогии с [lat, lng] в Leaflet CRS.Simple).

CREATE TABLE site_plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    image_file    TEXT    NOT NULL,   -- имя файла на диске, генерируем МЫ
    image_width   INTEGER NOT NULL,
    image_height  INTEGER NOT NULL,
    is_default    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE plan_zones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL REFERENCES site_plans(id) ON DELETE CASCADE,
    group_id    INTEGER NOT NULL REFERENCES meter_groups(id) ON DELETE CASCADE,
    shape_type  TEXT    NOT NULL DEFAULT 'polygon',  -- polygon | marker
    geometry    TEXT    NOT NULL,   -- JSON: [[y,x],...] либо [y,x]
    anchor      TEXT,               -- JSON [y,x]: точка привязки связей и подписи
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_plan_zones_plan_group ON plan_zones(plan_id, group_id);

CREATE TABLE plan_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id         INTEGER NOT NULL REFERENCES site_plans(id) ON DELETE CASCADE,
    from_zone_id    INTEGER NOT NULL REFERENCES plan_zones(id) ON DELETE CASCADE,
    to_zone_id      INTEGER NOT NULL REFERENCES plan_zones(id) ON DELETE CASCADE,
    source_meter_id INTEGER REFERENCES meters(id) ON DELETE SET NULL,
    rated_current_a REAL,           -- допустимый ток кабеля, необязательно
    waypoints       TEXT,           -- JSON [[y,x],...] — изломы трассы
    label           TEXT,           -- «АВВГ 4х95, 120 м»
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX idx_plan_links_plan ON plan_links(plan_id);

INSERT INTO schema_migrations (version, name, applied_at)
VALUES (4, 'site_plan', strftime('%s','now'));

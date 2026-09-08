-- Migration 003: нормализованные имена зон + цвет зоны
--
-- COLLATE NOCASE в SQLite сворачивает регистр только для ASCII A-Z,
-- поэтому "Цех1" и "ЦЕХ1" считались двумя разными зонами (A4 в ТЗ v0.8.0).
-- Правильная казефолд-нормализация с кириллицей возможна только на
-- стороне Python, поэтому здесь используется функция py_casefold(),
-- зарегистрированная в db.py перед применением миграций.

ALTER TABLE meter_groups ADD COLUMN name_norm TEXT;
ALTER TABLE meter_groups ADD COLUMN color TEXT;

-- Заполняем name_norm для существующих строк.
UPDATE meter_groups SET name_norm = py_casefold(name) WHERE name_norm IS NULL;

-- Разруливаем дубликаты: если несколько зон схлопнулись в один name_norm
-- (например "Цех2" и "ЦЕХ2" уже успели быть созданы раздельно из-за
-- бага NOCASE), у всех кроме самой старой (минимальный id) добавляем
-- суффикс "_<id>", чтобы уникальный индекс ниже не упал на существующих
-- данных. Сами зоны и привязка счётчиков к ним не удаляются и не
-- меняются — это только доборматизация индекса, разбор дублей — на
-- усмотрение администратора через UI (переименование с merge:true).
UPDATE meter_groups
SET name_norm = name_norm || '_' || id
WHERE id NOT IN (
    SELECT MIN(id) FROM meter_groups GROUP BY name_norm
);

CREATE UNIQUE INDEX idx_groups_name_norm ON meter_groups(name_norm);

INSERT INTO schema_migrations (version, name, applied_at)
VALUES (3, 'group_name_normalized', strftime('%s','now'));

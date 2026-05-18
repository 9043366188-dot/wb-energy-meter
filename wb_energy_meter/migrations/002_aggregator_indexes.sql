-- Migration 002: aggregator-related indexes
-- Step 4 adds heavy reads of period_aggregates by (meter_id, period_start).

-- Композитный индекс под основные выборки агрегатов:
--   * list_range(meter_id, ts_from, ts_to)
--   * earliest_hour / latest_hour
--   * missing_hours
--   * sum_kwh
CREATE INDEX IF NOT EXISTS idx_aggr_meter_period
    ON period_aggregates(meter_id, period_type, period_start);

-- Старый одиночный индекс по period_start оставляем — он покрывает
-- запросы "статистика за период по всем счётчикам" без фильтра по meter_id.

INSERT INTO schema_migrations (version, name, applied_at)
VALUES (2, 'aggregator_indexes', strftime('%s','now'));

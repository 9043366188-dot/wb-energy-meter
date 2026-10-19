-- Migration 006: пофазные часовые итоги (партия находок 0.22.3, П9).
-- docs/design-phase-aggregates.md.
--
-- Одна строка = один час одной фазы (L1, L2, L3) одного прибора. Колонки те же,
-- что у period_aggregates, чтобы один и тот же расчёт часа (compute_hourly_aggregate)
-- и один и тот же репозиторий обслуживали и итог прибора, и его фазы.
--
-- period_aggregates НЕ меняется: код предыдущей версии пишет в неё по ключу
-- (meter_id, period_type, period_start), и при откате этот ключ обязан остаться.
-- Новая таблица прежнему коду не видна.
CREATE TABLE phase_aggregates (
    meter_id        INTEGER NOT NULL REFERENCES meters(id) ON DELETE CASCADE,
    phase           TEXT    NOT NULL CHECK (phase IN ('L1', 'L2', 'L3')),
    period_start    INTEGER NOT NULL,
    period_end      INTEGER NOT NULL,
    ap_energy_start REAL,
    ap_energy_end   REAL,
    ap_energy_delta REAL,
    p_avg           REAL,
    p_max           REAL,
    samples_count   INTEGER,
    quality_flag    TEXT    NOT NULL DEFAULT 'ok',
    computed_at     INTEGER NOT NULL,
    PRIMARY KEY (meter_id, phase, period_start)
);

CREATE INDEX idx_phase_aggr_period_start ON phase_aggregates(period_start);

INSERT INTO schema_migrations (version, name, applied_at)
VALUES (6, 'phase_aggregates', strftime('%s','now'));

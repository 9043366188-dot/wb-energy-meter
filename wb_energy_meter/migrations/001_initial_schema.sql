-- Migration 001: initial schema
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    applied_at  INTEGER NOT NULL
);

CREATE TABLE meter_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    parent_id   INTEGER REFERENCES meter_groups(id) ON DELETE SET NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE meters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT    NOT NULL UNIQUE,
    display_name    TEXT    NOT NULL,
    group_id        INTEGER REFERENCES meter_groups(id) ON DELETE SET NULL,
    serial_number   TEXT,
    role            TEXT    NOT NULL DEFAULT 'consumer',
    enabled         INTEGER NOT NULL DEFAULT 1,
    notes           TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE INDEX idx_meters_device_id ON meters(device_id);
CREATE INDEX idx_meters_group_id  ON meters(group_id);
CREATE INDEX idx_meters_enabled   ON meters(enabled);

CREATE TABLE period_aggregates (
    meter_id        INTEGER NOT NULL REFERENCES meters(id) ON DELETE CASCADE,
    period_type     TEXT    NOT NULL,
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
    PRIMARY KEY (meter_id, period_type, period_start)
);

CREATE INDEX idx_aggr_period_start ON period_aggregates(period_start);

CREATE TABLE alert_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type   TEXT    NOT NULL,
    meter_id    INTEGER REFERENCES meters(id) ON DELETE SET NULL,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    status      TEXT    NOT NULL DEFAULT 'active',
    detail      TEXT,
    last_email_at INTEGER
);

CREATE INDEX idx_alerts_active     ON alert_events(status) WHERE status = 'active';
CREATE INDEX idx_alerts_meter      ON alert_events(meter_id, started_at);
CREATE INDEX idx_alerts_started_at ON alert_events(started_at);

CREATE TABLE snoozes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    INTEGER REFERENCES meters(id) ON DELETE CASCADE,
    rule_type   TEXT,
    until_ts    INTEGER NOT NULL,
    reason      TEXT,
    created_at  INTEGER NOT NULL
);

CREATE INDEX idx_snoozes_until ON snoozes(until_ts);

CREATE TABLE kv (
    key         TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL,
    updated_at  INTEGER NOT NULL
);

INSERT INTO schema_migrations (version, name, applied_at)
VALUES (1, 'initial_schema', strftime('%s','now'));

"""Тесты Шага 75 (партия 15, этап 15.6, A44): границы периода строит сервер по `timezone` запроса.

Находка партии 12: «ГГГГ-ММ-ДД» в запросах расчётов читалось в часовом поясе процесса службы,
`timezone` только возвращался подписью. Решение — `wb_energy_meter/timebounds.py`: строка-дата
читается в поясе запроса. Ожидания посчитаны вручную (Europe/Berlin, 2026):

  весна: 29.03.2026 00:00 CET (+01:00) = 28.03 23:00Z; 30.03.2026 00:00 CEST (+02:00) = 29.03 22:00Z
         → сутки 29.03 = 23 часа
  осень: 25.10.2026 00:00 CEST (+02:00) = 24.10 22:00Z; 26.10.2026 00:00 CET (+01:00) = 25.10 23:00Z
         → сутки 25.10 = 25 часов
  обычные сутки (22.10.2026): 24 часа
  Europe/Moscow (+03:00 круглый год): 2026-10-22 = 21.10 21:00Z
  несуществующее 02:30 29.03.2026 Berlin → смещение до перехода (+01:00) = 01:30Z
  повторяющееся 02:30 25.10.2026 Berlin → первое вхождение (CEST, +02:00) = 00:30Z

Каждый час 1.0 кВт·ч → значение суток равно числу часов (23.0 / 25.0 / 24.0).

Самостоятельный скрипт: python tests/test_step75_period_timezone.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, HERE)

from wb_energy_meter.timebounds import (  # noqa: E402
    parse_boundary, local_iso, resolve_zone, UnknownTimezone,
)
from test_step49_acceptance_gaps import Rig, make_client, approx, HOUR  # noqa: E402

BERLIN = "Europe/Berlin"


def _z(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


def test_boundaries_by_zone_not_by_process():
    # пояс процесса не должен влиять: пробуем три разных
    old_tz = os.environ.get("TZ")
    try:
        results = []
        for proc_tz in ("UTC", "Asia/Tokyo", "America/Los_Angeles"):
            os.environ["TZ"] = proc_tz
            time.tzset()
            results.append((
                parse_boundary("2026-03-29", BERLIN),
                parse_boundary("2026-03-30", BERLIN),
                parse_boundary("2026-10-25", BERLIN),
                parse_boundary("2026-10-26", BERLIN),
                parse_boundary("2026-10-22", "Europe/Moscow"),
                parse_boundary("2026-10-22", "UTC"),
                parse_boundary("2026-10-22", None),
            ))
        assert results[0] == results[1] == results[2], results
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    spring_a, spring_b, autumn_a, autumn_b, msk, utc, default = results[0]
    assert spring_a == _z(2026, 3, 28, 23), spring_a
    assert spring_b == _z(2026, 3, 29, 22), spring_b
    assert spring_b - spring_a == 23 * HOUR
    assert autumn_a == _z(2026, 10, 24, 22), autumn_a
    assert autumn_b == _z(2026, 10, 25, 23), autumn_b
    assert autumn_b - autumn_a == 25 * HOUR
    assert msk == _z(2026, 10, 21, 21), msk
    assert utc == _z(2026, 10, 22) and default == utc
    assert parse_boundary("2026-10-23", BERLIN) - parse_boundary("2026-10-22", BERLIN) == 24 * HOUR
    print("[OK] A44: границы суток строятся по timezone запроса при любом поясе процесса; "
          "Berlin: 23 ч (29.03), 25 ч (25.10), 24 ч обычные; Moscow = +03:00; без пояса — UTC")


def test_gap_and_ambiguous_wall_times_are_deterministic():
    assert parse_boundary("2026-03-29 02:30", BERLIN) == _z(2026, 3, 29, 1, 30)   # смещение до перехода
    assert parse_boundary("2026-10-25 02:30", BERLIN) == _z(2026, 10, 25, 0, 30)  # первое вхождение
    for text in ("2026-10-22T05:07", "2026-10-22 05:07:09", "2026-10-22 05:07", "  2026-10-22  "):
        parse_boundary(text, BERLIN)
    for bad in ("", "   ", "22.10.2026", "2026-13-01", "вчера"):
        try:
            parse_boundary(bad, BERLIN)
        except UnknownTimezone:
            raise AssertionError("пояс известен: %r" % (bad,))
        except ValueError:
            continue
        raise AssertionError("должна быть ошибка: %r" % (bad,))
    for bad_tz in ("Mars/Olympus", "../etc/passwd", 5):
        try:
            parse_boundary("2026-10-22", bad_tz)
        except UnknownTimezone:
            continue
        raise AssertionError("неизвестный пояс принят: %r" % (bad_tz,))
    assert resolve_zone(None) is resolve_zone("UTC")
    print("[OK] A44: пропуск и повтор часа читаются однозначно; плохие даты и пояса — ValueError")


def test_local_iso_labels_show_offset_change():
    a = parse_boundary("2026-03-29", BERLIN)
    b = parse_boundary("2026-03-30", BERLIN)
    assert local_iso(a, BERLIN) == "2026-03-29T00:00:00+01:00", local_iso(a, BERLIN)
    assert local_iso(b, BERLIN) == "2026-03-30T00:00:00+02:00", local_iso(b, BERLIN)
    assert local_iso(str(a), BERLIN) == "2026-03-29T00:00:00+01:00"        # строкой, как в ответах
    assert local_iso(str(float(a)), BERLIN) == "2026-03-29T00:00:00+01:00"
    assert local_iso(a, "UTC") == "2026-03-28T23:00:00+00:00"
    assert local_iso(a, "Нет/Такого") == "2026-03-28T23:00:00+00:00"       # подпись не роняет ответ
    assert local_iso("abc", BERLIN) is None and local_iso(None, BERLIN) is None
    print("[OK] A44: подписи from_local/to_local несут смещение пояса (+01:00 → +02:00 у суток перехода)")


def _fill(rig, meter_id, t_from, t_to):
    t = t_from
    while t < t_to:
        rig.add_hour_at(meter_id, t, 1.0)
        t += HOUR


def test_api_day_strings_give_23_24_25_hours():
    client, db, path, _registry = make_client()
    try:
        rig = Rig(db)
        m = rig.meters.add("tz", "tz")
        src = rig.sources.open_source(m.id, "wb8-main", "tz")
        p = rig.points.add("tz", "tz")
        rig.bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)
        _fill(rig, m.id, _z(2026, 3, 27), _z(2026, 3, 31))         # весна
        _fill(rig, m.id, _z(2026, 10, 21), _z(2026, 10, 28))       # осень и обычные дни

        def metric(t_from, t_to, tz=BERLIN):
            r = client.post("/api/v2/metrics/query", json={
                "mode": "measured", "point_ids": [p.id], "from": t_from, "to": t_to, "timezone": tz})
            return r

        for (d1, d2, hours) in (("2026-03-29", "2026-03-30", 23.0),
                                ("2026-10-25", "2026-10-26", 25.0),
                                ("2026-10-22", "2026-10-23", 24.0)):
            r = metric(d1, d2)
            assert r.status_code == 200, r.get_json()
            body = r.get_json()
            assert approx(body["value"], hours), (d1, body["value"], hours)
            # тот же период в unix-секундах — тот же результат
            r2 = metric(parse_boundary(d1, BERLIN), parse_boundary(d2, BERLIN))
            assert approx(r2.get_json()["value"], hours)
            assert body["period"]["from_local"].startswith(d1 + "T00:00:00"), body["period"]
            assert body["period"]["to_local"].startswith(d2 + "T00:00:00"), body["period"]
        spring = metric("2026-03-29", "2026-03-30").get_json()["period"]
        assert spring["from_local"].endswith("+01:00") and spring["to_local"].endswith("+02:00"), spring

        # соседние сутки не пересекаются: сумма двух суток = объединённый период
        a = metric("2026-10-25", "2026-10-26").get_json()["value"]
        b = metric("2026-10-26", "2026-10-27").get_json()["value"]
        ab = metric("2026-10-25", "2026-10-27").get_json()["value"]
        assert approx(a + b, ab) and approx(ab, 49.0), (a, b, ab)

        # тот же текст даты в другом поясе — другой момент
        berlin = metric("2026-10-22", "2026-10-23", BERLIN).get_json()["period"]
        moscow = metric("2026-10-22", "2026-10-23", "Europe/Moscow").get_json()["period"]
        assert berlin["from"] != moscow["from"]
        assert int(moscow["from"]) - int(berlin["from"]) == -1 * HOUR, (berlin, moscow)   # +03:00 против +02:00

        # без timezone — UTC, а не пояс процесса
        r = client.post("/api/v2/metrics/query", json={
            "mode": "measured", "point_ids": [p.id], "from": "2026-10-22", "to": "2026-10-23"})
        assert r.get_json()["period"]["from"] == str(_z(2026, 10, 22)), r.get_json()["period"]

        # неизвестный пояс: строка-дата → 400 по полю timezone; unix-секунды → как раньше
        r = metric("2026-10-22", "2026-10-23", "Mars/Olympus")
        assert r.status_code == 400 and r.get_json()["code"] == "bad_request", r.get_json()
        assert r.get_json()["fields"] == ["timezone"], r.get_json()
        r = metric(_z(2026, 10, 22), _z(2026, 10, 23), "Mars/Olympus")
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["period"]["timezone"] == "Mars/Olympus"
        assert r.get_json()["period"]["from_local"].endswith("+00:00")

        # обе остальные ручки расчётов читают даты так же
        r = client.post("/api/v2/overview/summary", json={
            "from": "2026-03-29", "to": "2026-03-30", "timezone": BERLIN})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["period"]["from_local"] == "2026-03-29T00:00:00+01:00", r.get_json()["period"]
        assert r.get_json()["period"]["to_local"] == "2026-03-30T00:00:00+02:00"
        r = client.post("/api/v2/reports/query", json={
            "dimension": "point", "scope_ids": [p.id], "from": "2026-10-25", "to": "2026-10-26",
            "timezone": BERLIN, "compare": {"from": "2026-10-22", "to": "2026-10-23"}})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert approx(body["rows"][0]["result"]["value"], 25.0), body["rows"][0]
        assert body["period"]["to_local"] == "2026-10-26T00:00:00+01:00", body["period"]
        assert body["compare_period"]["from_local"] == "2026-10-22T00:00:00+02:00", body["compare_period"]
        r = client.post("/api/v2/reports/query", json={
            "dimension": "point", "scope_ids": [p.id], "from": "2026-10-25", "to": "2026-10-26",
            "timezone": "Mars/Olympus"})
        assert r.status_code == 400 and r.get_json()["fields"] == ["timezone"], r.get_json()
        print("[OK] A44: metrics/query, overview/summary, reports/query: даты суток по timezone запроса — "
              "23 / 24 / 25 ч, соседние сутки без двойного учёта, подписи со смещением, "
              "неизвестный пояс при строке-дате → 400 по полю timezone")
    finally:
        db.close()
        os.unlink(path)


def test_change_log_date_filter_uses_timezone_param():
    client, db, path, _registry = make_client()
    try:
        # две записи журнала: 21.10 22:00Z (= 22.10 01:00 по Москве) и 22.10 10:00Z. Ожидания посчитаны вручную:
        #   from=2026-10-22 в Москве = 21.10 21:00Z → обе записи; то же в UTC = 22.10 00:00Z → только вторая;
        #   to=2026-10-22 в Москве = 21.10 21:00Z → ни одной (первая позже границы).
        t_early = int(datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc).timestamp())
        t_late = int(datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc).timestamp())
        with db.transaction() as conn:
            for n, t in enumerate((t_early, t_late), 1):
                conn.execute(
                    "INSERT INTO change_log (entity_type, entity_id, action, effective_from, recorded_at) "
                    "VALUES ('metering_point', ?, 'update', ?, ?)", (n, t, t))

        def ids(query):
            r_ = client.get("/api/v2/change-log?" + query)
            assert r_.status_code == 200, r_.get_json()
            return sorted(i["entity_id"] for i in r_.get_json()["items"] if i["entity_type"] == "metering_point")

        assert ids("from=2026-10-22&timezone=Europe/Moscow") == [1, 2], "в Москве 22.10 начинается раньше первой записи"
        assert ids("from=2026-10-22") == [2], "без timezone — UTC: первая запись раньше 22.10 00:00Z"
        assert ids("from=2026-10-22&timezone=UTC") == [2]
        assert ids("to=2026-10-22&timezone=Europe/Moscow") == [], "граница to в Москве — 21.10 21:00Z, обе записи позже"
        assert ids("to=2026-10-22&timezone=UTC") == [1], "в UTC граница to — 22.10 00:00Z: первая запись раньше"
        r = client.get("/api/v2/change-log?from=2026-10-22&to=2026-10-23&timezone=Europe/Moscow")
        assert r.status_code == 200, r.get_json()
        r = client.get("/api/v2/change-log?from=2026-10-22&timezone=Mars/Olympus")
        assert r.status_code == 400 and r.get_json()["fields"] == ["timezone"], r.get_json()
        r = client.get("/api/v2/change-log?from=1792618800&timezone=Mars/Olympus")   # unix — пояс не нужен
        assert r.status_code == 200, r.get_json()
        print("[OK] A44: журнал изменений читает даты в поясе параметра timezone")
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    test_boundaries_by_zone_not_by_process()
    test_gap_and_ambiguous_wall_times_are_deterministic()
    test_local_iso_labels_show_offset_change()
    test_api_day_strings_give_23_24_25_hours()
    test_change_log_date_filter_uses_timezone_param()
    print("\nВсе тесты Шага 75 (A44, границы периода по timezone) пройдены.")

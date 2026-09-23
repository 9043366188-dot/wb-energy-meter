"""Воспроизведение: небаланс на «Обзоре» считается по корневым учётным
группам, а не по электрической схеме.

Запуск из корня репозитория wb-energy-meter:
    python review-repro/repro_imbalance.py      (или положить рядом и поправить путь)

Ожидаемый вывод на 0.17.0 (1883664):
    groups=False overlap=False: total=100.0 imbalance=100.0 (100.0%)   <- схема верная, групп нет -> «небаланс 100%»
    groups=True  overlap=False: total=100.0 imbalance=10.0  (10.0%)    <- совпадает с правильным ответом случайно
    groups=True  overlap=True : total=100.0 imbalance=-50.0 (-50.0%)   <- точка в двух корневых группах -> двойной счёт

Правильный ответ во всех трёх случаях по ТЗ §5.2: 100 − (60 + 30) = 10 (10%),
потому что выходы границы — первые измерения вниз по отходящим линиям
щита, а не члены групп.
"""
import os
import sys

ROOT = os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)

import test_step39_plan_v3 as t  # noqa: E402  (хелперы сквозного теста «Плана v3»)

HOUR = t.HOUR


def scenario(with_group, overlap=False):
    client, db, path = t.make_client()
    try:
        vv = t._make_node(client, "n-vvod", "Ввод", "source")
        sh = t._make_node(client, "n-shr1", "ЩР-1", "panel")
        e_in = t._connect(client, vv["id"], sh["id"]).get_json()
        e1 = t._add_consumer(client, sh["id"], "Станки").get_json()["edge"]
        e2 = t._add_consumer(client, sh["id"], "Освещение").get_json()["edge"]
        pin = t._make_point(client, "vvod", "Ввод")
        p1 = t._make_point(client, "st", "Станки")
        p2 = t._make_point(client, "os", "Освещение")
        for p, dev in ((pin, "dev-v"), (p1, "dev-1"), (p2, "dev-2")):
            t._bind_meter(client, p["id"], dev, dev)
        t._set_meter_on_edge(client, e_in["id"], pin["id"])
        t._set_meter_on_edge(client, e1["id"], p1["id"])
        t._set_meter_on_edge(client, e2["id"], p2["id"])
        t.seed_energy(db, "dev-v", 100.0)
        t.seed_energy(db, "dev-1", 60.0)
        t.seed_energy(db, "dev-2", 30.0)
        if with_group:
            groups = [("Цех", [p1, p2])]
            if overlap:
                groups.append(("Арендатор А", [p1]))  # ТЗ §4.4 разрешает точке быть в нескольких группах
            for name, members in groups:
                g = client.post("/api/v2/groups", json={
                    "name": name, "expected_revision": t.current_rev(client)}).get_json()
                for p in members:
                    client.post(f"/api/v2/groups/{g['id']}/members", json={
                        "point_id": p["id"], "expected_revision": t.current_rev(client)})
        b = client.post("/api/v2/overview/summary",
                        json={"from": 0, "to": HOUR, "timezone": "UTC"}).get_json()
        print(f"groups={with_group!s:5} overlap={overlap!s:5}: "
              f"total={b['object_total']['value']} imbalance={b['imbalance_value']} "
              f"({b['imbalance_percent']}%)")
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    scenario(False)
    scenario(True)
    scenario(True, overlap=True)

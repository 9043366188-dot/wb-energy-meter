"""Тесты партии 14 (A30): замена фона плана этажа.

Самостоятельный скрипт (не pytest):
    python tests/test_step71_plan_background.py

A30 — «новый фон другого разрешения, затем обрезанный/повёрнутый фон»: первый меняется через
предпросмотр (ничего не пишется, значки пересчитываются пропорционально после подтверждения),
второй требует перепривязки (значки не двигаются сами, пропорциональный пересчёт отклонён), старый
фон можно вернуть, версии не затирают друг друга.
"""

from __future__ import annotations

import io
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "browser"))

from harness import Harness  # noqa: E402


def make_png(width, height):
    # минимальный PNG: сигнатура + IHDR; размеры из заголовка читает сервер (image_meta)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    return sig + ihdr


def upload(png):
    return (io.BytesIO(png), "bg.png")


def create_floor(h, name, w, hgt):
    r = h.client.post("/api/v2/plans", data={
        "name": name, "plan_kind": "floor", "file": upload(make_png(w, hgt))},
        content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def get(h, path, ok=200):
    r = h.client.get(path)
    assert r.status_code == ok, (path, r.status_code, r.get_json())
    return r.get_json()


def post_image(h, plan_id, w, hgt, **form):
    return h.client.post(f"/api/v2/plans/{plan_id}/image",
                         data={"file": upload(make_png(w, hgt)), **form},
                         content_type="multipart/form-data")


def preview(h, plan_id, w, hgt):
    r = h.client.post(f"/api/v2/plans/{plan_id}/image/preview",
                      data={"file": upload(make_png(w, hgt))},
                      content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def build(h):
    """План 800×600: два узла и линия между ними с изломом."""
    plan = create_floor(h, "Этаж", 800, 600)
    a = h.make_node("A", "Щит A", "panel")
    b = h.make_node("B", "Щит B", "panel")
    edge = h.connect(a["id"], b["id"])
    items = {}
    for key, node, xy in (("a", a, (400, 300)), ("b", b, (700, 500))):
        r = h.client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "node", "node_id": node["id"], "geometry": {"x": xy[0], "y": xy[1]},
            "coord_space": "image_px_xy_v2"})
        assert r.status_code == 201, r.get_json()
        items[key] = r.get_json()
    r = h.client.post(f"/api/v2/plans/{plan['id']}/edges", json={
        "edge_id": edge["id"], "from_item_id": items["a"]["id"], "to_item_id": items["b"]["id"],
        "waypoints": [[400, 300], [400, 500], [700, 500]]})
    assert r.status_code in (200, 201), r.get_json()
    return plan, items


def positions(h, plan_id):
    body = get(h, f"/api/v2/plans/{plan_id}")
    pos = {i["id"]: (i["geometry"]["x"], i["geometry"]["y"]) for i in body["items"]}
    wps = [e["waypoints"] for e in body["edges"]]
    return body, pos, wps


def test_preview_writes_nothing():
    with Harness() as h:
        plan, items = build(h)
        pid = plan["id"]
        before = positions(h, pid)
        rev = before[0]["canvas_revision"]

        p = preview(h, pid, 1600, 1200)
        assert p["compat"] == "scalable" and p["scale_x"] == 2.0 and p["scale_y"] == 2.0, p
        assert p["old"] == {"width": 800, "height": 600} and p["new"] == {"width": 1600, "height": 1200}
        assert p["items_total"] == 2 and p["items_outside_if_kept"] == 0, p

        small = preview(h, pid, 400, 300)  # вдвое меньше: узел B (700,500) окажется за краем
        assert small["compat"] == "scalable" and small["scale_x"] == 0.5, small
        assert small["items_outside_if_kept"] == 1, small

        crop = preview(h, pid, 600, 600)
        assert crop["compat"] == "reanchor", crop
        assert crop["items_outside_if_kept"] == 1, crop  # B (700,500) вне 600×600; A (400,300) внутри

        same = preview(h, pid, 800, 600)
        assert same["compat"] == "same", same

        after = positions(h, pid)
        assert after[0]["image_width"] == 800 and after[0]["canvas_revision"] == rev
        assert after[1] == before[1] and after[2] == before[2]
        assert len(get(h, f"/api/v2/plans/{pid}/image-versions")) == 1
        bad = h.client.post(f"/api/v2/plans/{pid}/image/preview",
                            data={"file": upload(b"not an image")}, content_type="multipart/form-data")
        assert bad.status_code == 400, bad.get_json()
        nofile = h.client.post(f"/api/v2/plans/{pid}/image/preview", data={},
                               content_type="multipart/form-data")
        assert nofile.status_code == 400
        print("[OK] предпросмотр: коэффициенты и «за краем» посчитаны, в базе ничего не изменилось")


def test_scale_replace_keeps_items_on_the_same_spot_of_the_drawing():
    with Harness() as h:
        plan, items = build(h)
        pid = plan["id"]
        rev0 = get(h, f"/api/v2/plans/{pid}")["canvas_revision"]
        r = post_image(h, pid, 1600, 1200, mode="scale")
        assert r.status_code == 200, r.get_json()
        body, pos, wps = positions(h, pid)
        assert (body["image_width"], body["image_height"]) == (1600, 1200)
        assert pos[items["a"]["id"]] == (800, 600) and pos[items["b"]["id"]] == (1400, 1000), pos
        assert wps == [[[800, 600], [800, 1000], [1400, 1000]]], wps
        assert body["canvas_revision"] == rev0 + 1, "устаревший редактор должен получить 409"

        # устаревшая ревизия отклоняется — чужая раскладка не затирает пересчитанную
        stale = h.client.post(f"/api/v2/plans/{pid}/layout", json={"expected_revision": rev0, "item_ops": []})
        assert stale.status_code == 409, stale.get_json()

        # вдвое меньше: возвращаемся к исходным координатам
        r = post_image(h, pid, 800, 600, mode="scale")
        assert r.status_code == 200
        _body, pos, wps = positions(h, pid)
        assert pos[items["a"]["id"]] == (400, 300) and pos[items["b"]["id"]] == (700, 500), pos
        assert wps == [[[400, 300], [400, 500], [700, 500]]], wps
        print("[OK] mode=scale: значки и изломы пересчитаны пропорционально, ревизия выросла")


def test_cropped_background_requires_reanchor():
    with Harness() as h:
        plan, items = build(h)
        pid = plan["id"]
        before = positions(h, pid)

        # пропорциональный пересчёт для обрезанного фона невозможен — ничего не меняется
        r = post_image(h, pid, 600, 600, mode="scale")
        assert r.status_code == 400 and r.get_json()["code"] == "reanchor_required", r.get_json()
        after = positions(h, pid)
        assert after[0]["image_width"] == 800 and after[1] == before[1] and after[2] == before[2]
        assert len(get(h, f"/api/v2/plans/{pid}/image-versions")) == 1

        # по умолчанию (keep) значки остаются на прежних пикселях; B окажется за краем
        r = post_image(h, pid, 600, 600)
        assert r.status_code == 200, r.get_json()
        body, pos, _wps = positions(h, pid)
        assert (body["image_width"], body["image_height"]) == (600, 600)
        assert pos[items["a"]["id"]] == (400, 300) and pos[items["b"]["id"]] == (700, 500), pos

        # перепривязка: сохранить B внутри новой картинки можно, старое положение — нельзя
        rev = body["canvas_revision"]
        bad = h.client.post(f"/api/v2/plans/{pid}/layout", json={"expected_revision": rev, "item_ops": [
            {"op": "upsert", "id": items["b"]["id"], "geometry": {"x": 700, "y": 500},
             "coord_space": "image_px_xy_v2", "label": None, "sort_order": 0}]})
        assert bad.status_code == 400, bad.get_json()
        ok = h.client.post(f"/api/v2/plans/{pid}/layout", json={"expected_revision": rev, "item_ops": [
            {"op": "upsert", "id": items["b"]["id"], "geometry": {"x": 500, "y": 450},
             "coord_space": "image_px_xy_v2", "label": None, "sort_order": 0}]})
        assert ok.status_code == 200, ok.get_json()
        print("[OK] обрезанный фон: пересчёт отклонён, значки на месте, перепривязка проходит")


def test_versions_list_and_restore():
    with Harness() as h:
        plan, items = build(h)
        pid = plan["id"]
        v1 = get(h, f"/api/v2/plans/{pid}/image-versions")
        assert len(v1) == 1 and v1[0]["is_current"] and (v1[0]["image_width"], v1[0]["image_height"]) == (800, 600)
        first_bytes = h.client.get(f"/api/v2/plans/{pid}/image").data
        first_id = v1[0]["id"]

        # две замены подряд (в одну секунду) не затирают файл предыдущей версии
        assert post_image(h, pid, 1600, 1200, mode="scale").status_code == 200
        assert post_image(h, pid, 600, 600).status_code == 200
        versions = get(h, f"/api/v2/plans/{pid}/image-versions")
        assert [v["is_current"] for v in versions] == [True, False, False], versions
        assert [(v["image_width"], v["image_height"]) for v in versions] == [(600, 600), (1600, 1200), (800, 600)]
        # у не текущих — что произойдёт при возврате
        assert versions[1]["compat"] == "reanchor" and versions[2]["compat"] == "reanchor", versions
        assert versions[0].get("compat") is None

        by_version = {v["id"]: h.client.get(f"/api/v2/plans/{pid}/image?version={v['id']}").data for v in versions}
        assert len(set(by_version.values())) == 3, "файлы версий должны быть разными"
        assert by_version[first_id] == first_bytes, "исходная версия должна сохраниться байт в байт"
        assert h.client.get(f"/api/v2/plans/{pid}/image?version=99999").status_code == 404
        other = create_floor(h, "Другой", 100, 100)
        assert h.client.get(f"/api/v2/plans/{other['id']}/image?version={first_id}").status_code == 404, \
            "версию чужого плана отдавать нельзя"

        # возврат: масштаб при другом соотношении сторон отклонён, keep — проходит
        r = h.client.post(f"/api/v2/plans/{pid}/image/restore", json={"version_id": first_id, "mode": "scale"})
        assert r.status_code == 400 and r.get_json()["code"] == "reanchor_required", r.get_json()
        r = h.client.post(f"/api/v2/plans/{pid}/image/restore", json={"version_id": first_id})
        assert r.status_code == 200, r.get_json()
        assert (r.get_json()["image_width"], r.get_json()["image_height"]) == (800, 600)
        assert h.client.get(f"/api/v2/plans/{pid}/image").data == first_bytes
        versions = get(h, f"/api/v2/plans/{pid}/image-versions")
        assert len(versions) == 3 and [v["is_current"] for v in versions] == [False, False, True], versions

        for body in ({"version_id": 99999}, {"version_id": first_id, "mode": "zoom"}, {}, {"version_id": "1"}):
            r = h.client.post(f"/api/v2/plans/{pid}/image/restore", json=body)
            assert r.status_code in (400, 404), (body, r.status_code)
        r = h.client.post(f"/api/v2/plans/{other['id']}/image/restore", json={"version_id": first_id})
        assert r.status_code == 404, "версия другого плана не возвращается"
        print("[OK] версии: список, чтение каждой, возврат (keep / scale), чужая версия недоступна")


def test_restore_with_scale_rescales_items():
    with Harness() as h:
        plan, items = build(h)
        pid = plan["id"]
        first_id = get(h, f"/api/v2/plans/{pid}/image-versions")[0]["id"]
        # новый фон вдвое крупнее, значки не трогаем (keep) — потом возвращаем старый фон с пересчётом
        assert post_image(h, pid, 1600, 1200).status_code == 200
        _b, pos, _w = positions(h, pid)
        assert pos[items["a"]["id"]] == (400, 300), pos
        versions = get(h, f"/api/v2/plans/{pid}/image-versions")
        old = next(v for v in versions if v["id"] == first_id)
        assert old["compat"] == "scalable" and old["scale_x"] == 0.5, old
        r = h.client.post(f"/api/v2/plans/{pid}/image/restore", json={"version_id": first_id, "mode": "scale"})
        assert r.status_code == 200, r.get_json()
        body, pos, wps = positions(h, pid)
        assert (body["image_width"], body["image_height"]) == (800, 600)
        assert pos[items["a"]["id"]] == (200, 150) and pos[items["b"]["id"]] == (350, 250), pos
        assert wps == [[[200, 150], [200, 250], [350, 250]]], wps
        print("[OK] возврат фона с mode=scale пересчитывает значки и изломы")


def test_delete_plan_removes_all_version_files():
    with Harness() as h:
        plan, _items = build(h)
        pid = plan["id"]
        assert post_image(h, pid, 1600, 1200, mode="scale").status_code == 200
        assert post_image(h, pid, 600, 600).status_code == 200
        names = [n for n in os.listdir(h.plans_dir) if n.startswith(f"plan_{pid}")]
        assert len(names) == 3, names
        assert h.client.delete(f"/api/v2/plans/{pid}").status_code == 200
        left = [n for n in os.listdir(h.plans_dir) if n.startswith(f"plan_{pid}")]
        assert not left, left
        print("[OK] удаление плана убирает файлы всех версий фона")


def main():
    test_preview_writes_nothing()
    test_scale_replace_keeps_items_on_the_same_spot_of_the_drawing()
    test_cropped_background_requires_reanchor()
    test_versions_list_and_restore()
    test_restore_with_scale_rescales_items()
    test_delete_plan_removes_all_version_files()
    print("\n[ALL OK] test_step71_plan_background")


if __name__ == "__main__":
    main()

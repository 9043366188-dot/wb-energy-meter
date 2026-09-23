"""Стенд для браузерных тестов «Плана v3» и остальных вкладок.

Этап 1 партии 7 (docs/TZ-batch7-review-fixes.md, §3 «Этап 1 —
браузерный стенд»). Поднимает НАСТОЯЩЕЕ Flask-приложение
(create_app + werkzeug make_server) в отдельном потоке на свободном
порту с временной БД — то же приложение, что видит браузер на объекте,
а не app.test_client(): Playwright умеет ходить только по настоящему
HTTP. HTTP-хелперы ниже (make_node/connect/... ) при этом бьют в
app.test_client() того же самого app — это быстрее прямых HTTP-вызовов
и не создаёт гонок с сервером в соседнем потоке (Flask/Werkzeug не
против параллельных запросов через test_client и через сокет к одному
и тому же приложению — они делят одну БД (SQLite, WAL) и один
in-memory MeterRegistry).

Устроено по образцу tests/test_step39_plan_v3.py::make_client /
make_client_with_plans (см. AGENTS.md — этот же паттерн _AppState).

Не pytest-фикстура: тесты этого каталога — самостоятельные скрипты
(python tests/browser/test_bNN_*.py), как и все остальные tests/*.py в
проекте (см. AGENTS.md → «Команды»). Harness — обычный контекст-менеджер:

    from harness import Harness, open_browser

    with Harness() as h:
        node = h.make_node("shr1", "ЩР-1", "panel")
        bp = open_browser(h.base_url)
        bp.page.goto(h.base_url + "#planv3")
        ...
        assert not bp.errors
        bp.close()
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from werkzeug.serving import make_server  # noqa: E402

from wb_energy_meter.db import Database  # noqa: E402
from wb_energy_meter.repo import GroupRepo, MeterRepo  # noqa: E402
from wb_energy_meter.alert_repo import AlertRepo  # noqa: E402
from wb_energy_meter.api import create_app, _AppState  # noqa: E402
from wb_energy_meter.model import MeterRegistry  # noqa: E402
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate  # noqa: E402
from wb_energy_meter.consumption import ConsumptionService  # noqa: E402
from wb_energy_meter.wb_db_client import RpcError  # noqa: E402
from wb_energy_meter.plan_repo import (  # noqa: E402
    SitePlanRepo, PlanZoneRepo, PlanLinkRepo,
)

HOUR = 3600
SHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_shots")


class _NoRpcDbClient:
    """Заглушка WbDbClient для браузерного стенда: в песочнице нет
    настоящего `wb-mqtt-db` (только голый mosquitto без RPC-обработчика),
    поэтому RPC-хвосты гибридного расчёта расхода (consumption.py)
    честно недоступны — как и на реальном контроллере при неотвечающем
    wb-mqtt-db. `RpcError` — уже предусмотренный в api.py путь (per-meter
    "no_data" в ответе, HTTP 200), а не сырой AttributeError/500."""

    def get_values(self, *args, **kwargs):
        raise RpcError("браузерный стенд: нет настоящего wb-mqtt-db, только агрегаты")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Harness:
    """Настоящий HTTP-сервер wb-energy-meter на временной БД."""

    def __init__(self, with_plans: bool = True):
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}/"
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.db_path)
        self.db = Database(path=self.db_path)
        self.db.open()
        self.groups_repo = GroupRepo(self.db)
        self.meters_repo = MeterRepo(self.db, self.groups_repo)
        self.registry = MeterRegistry()
        self.plans_dir = tempfile.mkdtemp() if with_plans else None
        self.aggregates_repo = AggregateRepo(self.db)
        # Легаси-«План» (вкладка «План» классического набора) и «Расход»
        # нужны настоящими объектами, а не None — иначе браузер получает
        # 503 на КАЖДОМ обновлении дашборда (он их не ждёт условно, а
        # опрашивает всегда) и тест обхода вкладок падает не из-за
        # реального бага, а из-за недособранного стенда.
        self.consumption_service = ConsumptionService(
            _NoRpcDbClient(), aggregates_repo=self.aggregates_repo,
            meters_repo=self.meters_repo,
        )
        self.state = _AppState(
            registry=self.registry, meters_repo=self.meters_repo,
            groups_repo=self.groups_repo, alert_repo=AlertRepo(self.db),
            is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
            mqtt_error_count=lambda: 0, wb_db_client=None,
            consumption_service=self.consumption_service,
            aggregates_repo=self.aggregates_repo,
            started_at=time.time(), db=self.db,
            plan_repo=SitePlanRepo(self.db),
            plan_zone_repo=PlanZoneRepo(self.db),
            plan_link_repo=PlanLinkRepo(self.db),
            plans_dir=self.plans_dir,
        )
        self.app = create_app(self.state)
        self.client = self.app.test_client()
        self._server = make_server("127.0.0.1", self.port, self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- жизненный цикл ---------------------------------------------
    def start(self) -> "Harness":
        self._thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("сервер harness не поднялся за 5 с")

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        self._thread.join(timeout=5)
        self.db.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def __enter__(self) -> "Harness":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # -- заведение топологии/точек через HTTP (test_client) ---------
    def current_revision(self) -> int:
        return self.client.get("/api/v2/revision").get_json()["configuration_revision"]

    def make_node(self, code, name, kind):
        rev = self.current_revision()
        r = self.client.post("/api/v2/topology/nodes", json={
            "code": code, "name": name, "kind": kind, "expected_revision": rev})
        assert r.status_code == 201, r.get_json()
        return r.get_json()

    def connect(self, from_id, to_id, **extra):
        rev = self.current_revision()
        body = {"from_node_id": from_id, "to_node_id": to_id, "expected_revision": rev}
        body.update(extra)
        r = self.client.post("/api/v2/topology/edges/connect", json=body)
        assert r.status_code == 201, r.get_json()
        return r.get_json()

    def add_consumer(self, node_id, name, **extra):
        rev = self.current_revision()
        body = {"name": name, "expected_revision": rev}
        body.update(extra)
        r = self.client.post(f"/api/v2/topology/nodes/{node_id}/add-consumer", json=body)
        assert r.status_code == 201, r.get_json()
        return r.get_json()

    def set_meter_on_edge(self, edge_id, point_id):
        rev = self.current_revision()
        r = self.client.patch(f"/api/v2/topology/edges/{edge_id}", json={
            "primary_point_id": point_id, "expected_revision": rev})
        assert r.status_code == 200, r.get_json()
        return r.get_json()

    def make_point(self, code, name):
        rev = self.current_revision()
        r = self.client.post("/api/v2/points", json={
            "code": code, "name": name, "expected_revision": rev})
        assert r.status_code == 201, r.get_json()
        return r.get_json()

    def bind_meter(self, point_id, device_id, display_name):
        rev = self.current_revision()
        r = self.client.post(f"/api/v2/points/{point_id}/bindings", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": device_id,
                          "display_name": display_name},
            "channel_profile": "total_3p", "valid_from": 0,
            "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()
        return r.get_json()

    def seed_energy(self, device_id, kwh, period_start=0, period_end=HOUR):
        """Засеять часовой агрегат прибору (device_id уже должен быть
        привязан к точке через bind_meter — см. tests/test_step39_plan_v3.py)."""
        meter = self.meters_repo.get_by_device_id(device_id)
        assert meter is not None, f"прибор {device_id} должен уже существовать (bind_meter)"
        AggregateRepo(self.db).upsert(HourlyAggregate(
            meter_id=meter.id, period_start=period_start, period_end=period_end,
            ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
            p_avg=None, p_max=None, samples_count=1, quality_flag="ok",
            computed_at=0))

    def see_in_mqtt(self, device_id, display_name=None):
        """Прибор «виден в MQTT», но ещё не привязан ни к одной точке —
        заполняет MeterRegistry напрямую, без настоящего брокера
        (браузерные сценарии не поднимают mosquitto — это не даемон/e2e
        юнит-тест, а HTTP+UI поверх готовых данных в БД)."""
        m = self.registry.get_or_create(device_id)
        if display_name:
            m.display_name = display_name
        return m

    def create_plan(self, name="Смоук"):
        # POST /api/v2/plans читает multipart/form-data (request.form —
        # см. api_v2.py::v2_plans, тот же контракт, что у v1 /api/plans),
        # НЕ JSON — с json=... "name" всегда приходил пустым и падал
        # "Имя плана не может быть пустым" (найдено при написании
        # test_b03_planv3_flow.py). Пустой однолинейный план — тот же
        # набор полей, что и planV3CreateEmptyPlan() во фронтенде.
        r = self.client.post("/api/v2/plans", data={
            "name": name, "plan_kind": "single_line",
            "canvas_width": "2000", "canvas_height": "1200"})
        assert r.status_code == 201, r.get_json()
        return r.get_json()


class BrowserPage:
    """Обёртка над Playwright Page: копит console.error/pageerror,
    хелпер для скриншотов в tests/browser/_shots/ (в .gitignore)."""

    def __init__(self, playwright, browser, page):
        self._playwright = playwright
        self._browser = browser
        self.page = page
        self.errors: list[str] = []
        page.on("console", self._on_console)
        page.on("pageerror", lambda e: self.errors.append(str(e)))

    def _on_console(self, msg) -> None:
        if msg.type == "error":
            self.errors.append(msg.text)

    def screenshot(self, name: str) -> str:
        os.makedirs(SHOTS_DIR, exist_ok=True)
        path = os.path.join(SHOTS_DIR, name if name.endswith(".png") else name + ".png")
        self.page.screenshot(path=path)
        return path

    def close(self) -> None:
        self._browser.close()
        self._playwright.stop()


def open_browser(url: str, viewport=None) -> BrowserPage:
    """Открыть страницу url в Chromium (PLAYWRIGHT_BROWSERS_PATH из
    окружения — см. AGENTS.md/README, playwright install не запускать)."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    page = browser.new_page(viewport=viewport or {"width": 1400, "height": 1000})
    bp = BrowserPage(pw, browser, page)
    page.goto(url)
    return bp

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

playwright_sync = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync.sync_playwright


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "persona_continuum" / "web" / "static"
CHROME_CANDIDATES = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
)


def _agent_payload() -> dict[str, Any]:
    return {
        "id": "fake_agent",
        "name": "Fake Test Agent",
        "status": "ready",
        "runtime_source": "local_cli",
        "definition_source": "builtin",
        "capabilities": {
            "web_search": True,
            "web_fetch": True,
            "model_discovery": True,
            "reasoning_discovery": True,
        },
        "models": [
            {
                "id": "fake-gpt-5",
                "display_name": "Fake GPT-5",
                "supported_reasoning_efforts": ["none", "low", "medium", "high"],
                "default_reasoning_effort": "high",
                "selectable": True,
            },
            {
                "id": "fake-claude-4",
                "display_name": "Fake Claude 4",
                "supported_reasoning_efforts": ["none", "low", "medium"],
                "default_reasoning_effort": "medium",
                "selectable": True,
            },
        ],
    }


def _profile_payload() -> dict[str, Any]:
    return {
        "id": "steve_jobs",
        "persona_id": "steve_jobs",
        "display_name": "Steve Jobs",
        "slug": "steve-jobs",
        "profile_type": "persona",
        "summary": "产品体验与端到端控制导向的技术领导者。",
        "status": "compiled",
        "compile_state": "compiled",
        "coverage_state": "complete",
        "source_count": 20,
        "evidence_count": 20,
        "version": 1,
        "updated_at": "2026-08-25T00:00:00Z",
        "coverage": {},
        "runtime_snapshot": {},
        "payload": {},
    }


class _FixtureState:
    def __init__(self) -> None:
        self.ready_runtime = True
        self.fail_agents = False
        self.enrichment_delay_seconds = 0.0
        self.enrichment_requests: list[dict[str, Any]] = []


class _FixtureServer(ThreadingHTTPServer):
    state: _FixtureState


class _FixtureHandler(BaseHTTPRequestHandler):
    server: _FixtureServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )

    def _send_static(self, relative: str) -> None:
        path = (STATIC / relative).resolve()
        if STATIC not in path.parents or not path.is_file():
            self._send_bytes(404, b"not found", "text/plain")
            return
        content_type = (
            "text/html; charset=utf-8"
            if path.suffix == ".html"
            else "text/javascript; charset=utf-8"
        )
        self._send_bytes(200, path.read_bytes(), content_type)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler protocol
        path = urlparse(self.path).path
        if path == "/":
            self._send_static("index.html")
            return
        if path.startswith("/static/"):
            self._send_static(path.removeprefix("/static/"))
            return
        if path == "/api/agents":
            if self.server.state.fail_agents:
                self._send_json(503, {"ok": False, "error": "agent scan unavailable"})
            else:
                agents = [_agent_payload()] if self.server.state.ready_runtime else []
                self._send_json(200, {"ok": True, "data": agents})
            return
        if path == "/api/profiles":
            self._send_json(200, {"ok": True, "data": [_profile_payload()]})
            return
        if path == "/api/profiles/steve_jobs":
            self._send_json(200, {"ok": True, "data": _profile_payload()})
            return
        if path in {"/api/personas", "/api/rooms", "/api/auth-profiles", "/api/worlds"}:
            self._send_json(200, {"ok": True, "data": []})
            return
        self._send_json(200, {"ok": True, "data": []})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler protocol
        path = urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        if path.endswith("/enrich"):
            payload = json.loads(raw_body.decode("utf-8"))
            self.server.state.enrichment_requests.append(payload)
            if self.server.state.enrichment_delay_seconds:
                time.sleep(self.server.state.enrichment_delay_seconds)
            self._send_json(
                202,
                {"ok": True, "data": {"id": "enrich_job_1", "status": "created"}},
            )
            return
        self._send_json(200, {"ok": True, "data": []})


@pytest.fixture()
def ui_server() -> Any:
    server = _FixtureServer(("127.0.0.1", 0), _FixtureHandler)
    server.state = _FixtureState()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture()
def browser_page(ui_server: _FixtureServer) -> Any:
    executable = next((path for path in CHROME_CANDIDATES if path.is_file()), None)
    if executable is None:
        pytest.skip("A locally installed Chromium-compatible browser is required")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{ui_server.server_port}/", wait_until="domcontentloaded")
        page.locator('[data-enrich-profile="steve_jobs"]').wait_for()
        yield page
        browser.close()


def _open_from_card(page: Page) -> None:
    page.locator('[data-enrich-profile="steve_jobs"]').click()
    dialog = page.locator("#dlg-profile-enrich")
    dialog.wait_for(state="visible")
    assert dialog.evaluate("(el) => el.open") is True


@pytest.mark.e2e
def test_click_enrich_persona_opens_dialog(browser_page: Page) -> None:
    _open_from_card(browser_page)
    assert browser_page.locator("#profile-enrich-target").inner_text() == "Steve Jobs · Persona"


@pytest.mark.e2e
def test_profile_enrichment_dialog_has_capability_element(browser_page: Page) -> None:
    assert browser_page.locator("#profile-enrich-capability").count() == 1
    _open_from_card(browser_page)
    assert "研究能力" in browser_page.locator("#profile-enrich-capability").inner_text()


@pytest.mark.e2e
def test_profile_enrichment_runtime_selector_initializes(browser_page: Page) -> None:
    _open_from_card(browser_page)
    assert browser_page.locator("#profile-enrich-agent option").count() == 1
    assert browser_page.locator("#profile-enrich-model option").count() == 2
    browser_page.locator("#profile-enrich-model").select_option("fake-claude-4")
    assert browser_page.locator("#profile-enrich-reasoning option").count() == 3


@pytest.mark.e2e
def test_profile_enrichment_without_capability_element_still_opens_dialog(
    browser_page: Page,
) -> None:
    browser_page.locator("#profile-enrich-capability").evaluate("(el) => el.remove()")
    _open_from_card(browser_page)


@pytest.mark.e2e
def test_profile_enrichment_no_ready_runtime_still_opens_dialog(
    browser_page: Page, ui_server: _FixtureServer
) -> None:
    ui_server.state.ready_runtime = False
    browser_page.reload(wait_until="domcontentloaded")
    browser_page.locator('[data-enrich-profile="steve_jobs"]').wait_for()
    _open_from_card(browser_page)
    assert browser_page.locator("#profile-enrich-submit").is_disabled()
    assert "没有可用 Runtime" in browser_page.locator("#profile-enrich-capability").inner_text()


@pytest.mark.e2e
def test_profile_enrichment_click_error_is_visible(
    browser_page: Page, ui_server: _FixtureServer
) -> None:
    ui_server.state.fail_agents = True
    browser_page.reload(wait_until="domcontentloaded")
    browser_page.locator('[data-enrich-profile="steve_jobs"]').wait_for()
    _open_from_card(browser_page)
    error = browser_page.locator("#profile-enrich-error")
    assert error.is_visible()
    assert "无法刷新 Runtime 列表" in error.inner_text()


@pytest.mark.e2e
def test_profile_detail_enrichment_button_opens_dialog(browser_page: Page) -> None:
    browser_page.locator(".persona-card h3").click()
    detail_dialog = browser_page.locator("#dlg-persona")
    detail_dialog.wait_for(state="visible")
    assert detail_dialog.evaluate("(el) => el.open") is True
    browser_page.locator("#pd-enrich").click()
    dialog = browser_page.locator("#dlg-profile-enrich")
    dialog.wait_for(state="visible")
    assert dialog.evaluate("(el) => el.open") is True


@pytest.mark.e2e
def test_profile_enrichment_submit_posts_to_enrich_endpoint(
    browser_page: Page, ui_server: _FixtureServer
) -> None:
    _open_from_card(browser_page)
    browser_page.locator("#profile-enrich-submit").click()
    browser_page.wait_for_function(
        "() => window.location.pathname === '/' "
        "&& document.querySelector('#dlg-profile-enrich')?.open === false"
    )
    assert len(ui_server.state.enrichment_requests) == 1
    request = ui_server.state.enrichment_requests[0]
    assert request["runtime"]["agent_id"] == "fake_agent"
    assert request["runtime"]["model_id"] == "fake-gpt-5"


@pytest.mark.e2e
def test_profile_enrichment_submit_has_immediate_busy_feedback(
    browser_page: Page, ui_server: _FixtureServer
) -> None:
    ui_server.state.enrichment_delay_seconds = 0.5
    _open_from_card(browser_page)

    submit = browser_page.locator("#profile-enrich-submit")
    submit.click()

    assert submit.is_disabled()
    assert submit.inner_text() == "正在创建升级任务…"
    status = browser_page.locator("#profile-enrich-submit-status")
    assert status.is_visible()
    assert "正在读取材料" in status.inner_text()
    browser_page.locator("#dlg-profile-enrich-progress").wait_for(state="visible")

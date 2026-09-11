import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_PRESENT = (ROOT / "data" / "processed" / "predictions.parquet").exists()


@pytest.fixture(scope="session")
def client():
    """FastAPI TestClient. Importing backend.main loads all model artifacts from
    disk at module-import time, so we only import it (and only once, session-scoped)
    once we know the data pipeline has actually been run."""
    if not ARTIFACTS_PRESENT:
        pytest.skip("data pipeline artifacts not present; run scripts/*.py first (see README)")
    from fastapi.testclient import TestClient
    from backend.main import app

    return TestClient(app)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server():
    """A real uvicorn process (not the ASGI TestClient) serving the actual frontend,
    for Playwright tests that need a browser to load real HTML/JS/CSS over HTTP."""
    if not ARTIFACTS_PRESENT:
        pytest.skip("data pipeline artifacts not present; run scripts/*.py first (see README)")

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            proc.terminate()
            pytest.fail("live_server did not start in time")
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    yield pg
    pg.close()

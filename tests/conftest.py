from pathlib import Path

import pytest

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

"""Shared pytest fixtures."""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ENGINE_DIR = Path(__file__).resolve().parents[1]

# Ensure the correlation-engine app is imported, not second-opinion's app.
sys.modules.pop("app", None)
sys.path.insert(0, str(ENGINE_DIR))

from app import app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def sample_payload() -> dict:
    """The brief section 3c incident, as Alertmanager would send it."""
    fixture = Path(__file__).parent / "fixtures" / "sample_alertmanager_payload.json"
    return json.loads(fixture.read_text(encoding="utf-8"))

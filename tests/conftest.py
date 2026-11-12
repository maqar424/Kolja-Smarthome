"""Shared test fixtures."""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def z2m_snapshot() -> dict:
    """Trimmed Zigbee2MQTT export (devices with exposes, groups, scenes)."""
    return json.loads((REPO_ROOT / "docs" / "z2m-snapshot.json").read_text(encoding="utf-8"))

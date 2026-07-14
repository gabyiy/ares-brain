import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def isolate_default_owner_profile(monkeypatch, tmp_path):
    """No automated test may write the canonical production owner profile."""

    monkeypatch.setenv(
        "ARES_OWNER_PROFILE_PATH",
        str(tmp_path / "isolated_default_owner_profile.json"),
    )

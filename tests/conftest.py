import datetime as dt
import shutil
from pathlib import Path

import pytest

from second_brain.people import People
from second_brain.storage import LocalStorage
from second_brain.vault import Vault

EXAMPLE = Path(__file__).parent.parent / "examples" / "vault"
TODAY = dt.date(2026, 1, 10)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "vault"
    shutil.copytree(EXAMPLE, dst)
    return dst


@pytest.fixture
def vault(vault_dir: Path) -> Vault:
    return Vault(LocalStorage(vault_dir), today=lambda: TODAY)


@pytest.fixture
def people(vault: Vault) -> People:
    return People(vault)


@pytest.fixture(autouse=True)
def _no_stats_from_env(monkeypatch):
    """Never record a test's tool calls into a database configured in the developer's env or .env."""
    monkeypatch.setenv("SB_STATS", "off")
    monkeypatch.delenv("SB_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

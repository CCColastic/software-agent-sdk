"""Named LLM profiles must not lose their key after a cipher key change.

Regression tests for #5317: under a changed key, activating a profile applied
a keyless LLM to settings, and re-saving it wrote ``null`` over the stored key.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from openhands.agent_server.api import create_app
from openhands.agent_server.config import Config
from openhands.agent_server.persistence import reset_stores
from openhands.sdk.llm import LLM
from openhands.sdk.llm.llm_profile_store import LLMProfileStore
from openhands.sdk.profiles import AgentProfileStore
from openhands.sdk.utils.cipher import Cipher


KEY_A = "qa-cipher-key-a"
KEY_B = "qa-cipher-key-b"
PROFILE = "qa-rotate"


@pytest.fixture
def dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        reset_stores()
        old_val = os.environ.get("OH_PERSISTENCE_DIR")
        os.environ["OH_PERSISTENCE_DIR"] = str(root / "settings")
        yield root
        reset_stores()
        if old_val is not None:
            os.environ["OH_PERSISTENCE_DIR"] = old_val
        else:
            os.environ.pop("OH_PERSISTENCE_DIR", None)


@pytest.fixture
def client_b(dirs: Path):
    """A server running under KEY_B."""
    config = Config(
        static_files_path=None, session_api_keys=[], secret_key=SecretStr(KEY_B)
    )
    with (
        patch(
            "openhands.agent_server.profiles_router.get_llm_profile_store",
            lambda: LLMProfileStore(base_dir=dirs / "profiles"),
        ),
        patch(
            "openhands.agent_server.settings_router.get_llm_profile_store",
            lambda: LLMProfileStore(base_dir=dirs / "profiles"),
        ),
        patch(
            "openhands.agent_server.profiles_router.get_agent_profile_store",
            lambda: AgentProfileStore(base_dir=dirs / "agent-profiles"),
        ),
    ):
        yield TestClient(create_app(config))


def _seed_profile_under_key_a(dirs: Path) -> bytes:
    store = LLMProfileStore(base_dir=dirs / "profiles")
    llm = LLM(usage_id="rotate", model="openai/qa-rotate", api_key=SecretStr("sk-a"))
    store.save(PROFILE, llm, include_secrets=True, cipher=Cipher(KEY_A))
    return (dirs / "profiles" / f"{PROFILE}.json").read_bytes()


def _profile_key_under_key_a(dirs: Path) -> str | None:
    llm = LLMProfileStore(base_dir=dirs / "profiles").load(
        PROFILE, cipher=Cipher(KEY_A)
    )
    return (
        llm.api_key.get_secret_value() if isinstance(llm.api_key, SecretStr) else None
    )


def test_activate_undecryptable_profile_refused(client_b: TestClient, dirs: Path):
    before = _seed_profile_under_key_a(dirs)
    settings_path = dirs / "settings" / "settings.json"
    assert not settings_path.exists()

    response = client_b.post(f"/api/profiles/{PROFILE}/activate")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Profile '{PROFILE}' is encrypted with a different key"
    )
    assert "gAAAAA" not in response.text
    # Nothing was written: no keyless LLM applied, profile file untouched.
    assert not settings_path.exists()
    assert (dirs / "profiles" / f"{PROFILE}.json").read_bytes() == before
    assert client_b.get("/api/profiles").json()["active_profile"] is None


def test_patch_active_undecryptable_profile_refused(client_b: TestClient, dirs: Path):
    """``PATCH /api/settings`` with ``active_profile`` is the same activation."""
    _seed_profile_under_key_a(dirs)

    response = client_b.patch("/api/settings", json={"active_profile": PROFILE})

    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Profile '{PROFILE}' is encrypted with a different key"
    )
    assert not (dirs / "settings" / "settings.json").exists()


def test_resave_undecryptable_profile_refused(client_b: TestClient, dirs: Path):
    before = _seed_profile_under_key_a(dirs)

    # Reads stay lenient: the key reads as unset under the wrong key.
    detail = client_b.get(
        f"/api/profiles/{PROFILE}", headers={"X-Expose-Secrets": "encrypted"}
    )
    assert detail.status_code == 200
    assert detail.json()["api_key_set"] is False
    config = detail.json()["config"]
    assert config["api_key"] is None

    # Saving that config back would write null over the stored key.
    response = client_b.post(f"/api/profiles/{PROFILE}", json={"llm": config})

    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Profile '{PROFILE}' is encrypted with a different key"
    )
    assert (dirs / "profiles" / f"{PROFILE}.json").read_bytes() == before
    assert _profile_key_under_key_a(dirs) == "sk-a"

    # Deleting stays possible, so a user can start over under the new key.
    assert client_b.delete(f"/api/profiles/{PROFILE}").status_code == 200
    saved = client_b.post(
        f"/api/profiles/{PROFILE}",
        json={"llm": {"model": "openai/qa-rotate", "api_key": "sk-b"}},
    )
    assert saved.status_code == 201
    stored = json.loads((dirs / "profiles" / f"{PROFILE}.json").read_text())
    assert Cipher(KEY_B).try_decrypt_str(stored["api_key"]) == "sk-b"

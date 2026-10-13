"""Settings and secrets writes must not destroy values stored under another key.

Regression tests for #5622: after a cipher key change, a read-modify-write used
to load undecryptable values as ``None`` and save them back as ``null``.
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from openhands.agent_server.api import create_app
from openhands.agent_server.config import Config
from openhands.agent_server.persistence import (
    FileSecretsStore,
    FileSettingsStore,
    PersistedSettings,
    reset_stores,
)
from openhands.agent_server.persistence.models import SettingsUpdatePayload
from openhands.sdk.llm import LLM
from openhands.sdk.utils.cipher import Cipher


KEY_A = "qa-cipher-key-a"
KEY_B = "qa-cipher-key-b"
LLM_KEY = "sk-test-original"
SECRET_VALUE = "token-original"


@pytest.fixture
def persistence_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        reset_stores()
        old_val = os.environ.get("OH_PERSISTENCE_DIR")
        os.environ["OH_PERSISTENCE_DIR"] = tmpdir
        yield Path(tmpdir)
        reset_stores()
        if old_val is not None:
            os.environ["OH_PERSISTENCE_DIR"] = old_val
        else:
            os.environ.pop("OH_PERSISTENCE_DIR", None)


def _apply(
    settings: PersistedSettings, payload: SettingsUpdatePayload
) -> PersistedSettings:
    settings.update(payload)
    return settings


def _seed(persistence_dir: Path) -> tuple[bytes, bytes]:
    """Store an LLM key and a custom secret under KEY_A; return the raw files."""
    cipher = Cipher(KEY_A)
    settings_store = FileSettingsStore(persistence_dir, cipher=cipher)
    settings_store.update(
        lambda s: _apply(s, {"agent_settings_diff": {"llm": {"api_key": LLM_KEY}}})
    )
    secrets_store = FileSecretsStore(persistence_dir, cipher=cipher)
    secrets_store.set_secret("QA_TOKEN", SECRET_VALUE)
    return (
        (persistence_dir / "settings.json").read_bytes(),
        (persistence_dir / "secrets.json").read_bytes(),
    )


def _assert_original_key_reads_back(persistence_dir: Path) -> None:
    cipher = Cipher(KEY_A)
    settings = FileSettingsStore(persistence_dir, cipher=cipher).load()
    assert settings is not None
    llm = settings.agent_settings.llm
    assert isinstance(llm, LLM)
    assert llm.api_key is not None
    assert isinstance(llm.api_key, SecretStr)
    assert llm.api_key.get_secret_value() == LLM_KEY
    secrets_store = FileSecretsStore(persistence_dir, cipher=cipher)
    assert secrets_store.get_secret("QA_TOKEN") == SECRET_VALUE


def test_settings_update_refuses_under_wrong_cipher(persistence_dir):
    settings_before, _ = _seed(persistence_dir)
    store = FileSettingsStore(persistence_dir, cipher=Cipher(KEY_B))

    with pytest.raises(RuntimeError, match="different key"):
        store.update(lambda s: _apply(s, {"misc_settings_diff": {"qa_note": True}}))

    assert (persistence_dir / "settings.json").read_bytes() == settings_before
    _assert_original_key_reads_back(persistence_dir)


@pytest.mark.parametrize(
    "write",
    [
        lambda store: store.set_secret("QA_OTHER", "x"),
        lambda store: store.delete_secret("QA_TOKEN"),
        lambda store: store.load_versioned_secret("QA_TOKEN"),
    ],
    ids=["set_secret", "delete_secret", "load_versioned_secret"],
)
def test_secrets_writes_refuse_under_wrong_cipher(persistence_dir, write):
    _, secrets_before = _seed(persistence_dir)
    store = FileSecretsStore(persistence_dir, cipher=Cipher(KEY_B))

    with pytest.raises(RuntimeError, match="different key"):
        write(store)

    assert (persistence_dir / "secrets.json").read_bytes() == secrets_before
    _assert_original_key_reads_back(persistence_dir)


def test_reads_stay_lenient_under_wrong_cipher(persistence_dir):
    _seed(persistence_dir)
    cipher = Cipher(KEY_B)

    settings = FileSettingsStore(persistence_dir, cipher=cipher).load()
    assert settings is not None
    assert isinstance(settings.agent_settings.llm, LLM)
    assert settings.agent_settings.llm.api_key is None
    secrets_store = FileSecretsStore(persistence_dir, cipher=cipher)
    assert secrets_store.load() is not None
    assert secrets_store.get_secret("QA_TOKEN") is None


def test_api_writes_refuse_under_wrong_cipher(persistence_dir):
    settings_before, secrets_before = _seed(persistence_dir)
    config = Config(
        static_files_path=None,
        session_api_keys=[],
        secret_key=SecretStr(KEY_B),
    )
    client = TestClient(create_app(config))

    patch = client.patch(
        "/api/settings", json={"misc_settings_diff": {"qa_note": True}}
    )
    assert patch.status_code == 409
    put = client.put("/api/settings/secrets", json={"name": "QA_OTHER", "value": "x"})
    assert put.status_code == 500
    delete = client.delete("/api/settings/secrets/QA_TOKEN")
    assert delete.status_code == 500
    for response in (patch, put, delete):
        assert "gAAAAA" not in response.text

    # Reads under the wrong key still answer.
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/settings/secrets").status_code == 200

    assert (persistence_dir / "settings.json").read_bytes() == settings_before
    assert (persistence_dir / "secrets.json").read_bytes() == secrets_before
    _assert_original_key_reads_back(persistence_dir)

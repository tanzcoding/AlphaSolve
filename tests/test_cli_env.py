"""Tests for cli._apply_env_sources (.env auto-loading + --env override).

Precedence (highest first):
  1. --env KEY=VAL flags (always win)
  2. Existing os.environ
  3. cwd .env
  4. user .env
"""
from __future__ import annotations

from pathlib import Path

import pytest

from alphasolve.cli import _apply_env_sources


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_loads_from_cwd_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_X", raising=False)
    cwd_env = tmp_path / ".env"
    _write(cwd_env, "TEST_X=from_cwd\n")
    _apply_env_sources(
        cwd_env_path=cwd_env,
        user_env_path=tmp_path / "nope.env",
        env_overrides=[],
    )
    import os
    assert os.environ["TEST_X"] == "from_cwd"


def test_loads_from_user_env_when_cwd_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_Y", raising=False)
    user_env = tmp_path / "user.env"
    _write(user_env, "TEST_Y=from_user\n")
    _apply_env_sources(
        cwd_env_path=tmp_path / "nope.env",
        user_env_path=user_env,
        env_overrides=[],
    )
    import os
    assert os.environ["TEST_Y"] == "from_user"


def test_existing_env_not_overridden_by_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_Z", "from_shell")
    cwd_env = tmp_path / ".env"
    _write(cwd_env, "TEST_Z=from_dotenv\n")
    _apply_env_sources(
        cwd_env_path=cwd_env,
        user_env_path=tmp_path / "nope.env",
        env_overrides=[],
    )
    import os
    assert os.environ["TEST_Z"] == "from_shell"


def test_cwd_dotenv_takes_priority_over_user(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_W", raising=False)
    user_env = tmp_path / "user.env"
    cwd_env = tmp_path / ".env"
    _write(user_env, "TEST_W=from_user\n")
    _write(cwd_env, "TEST_W=from_cwd\n")
    _apply_env_sources(
        cwd_env_path=cwd_env,
        user_env_path=user_env,
        env_overrides=[],
    )
    import os
    assert os.environ["TEST_W"] == "from_cwd"


def test_env_flag_overrides_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_A", "from_shell")
    _apply_env_sources(
        cwd_env_path=tmp_path / "nope.env",
        user_env_path=tmp_path / "nope2.env",
        env_overrides=["TEST_A=from_flag"],
    )
    import os
    assert os.environ["TEST_A"] == "from_flag"


def test_env_flag_overrides_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_B", raising=False)
    cwd_env = tmp_path / ".env"
    _write(cwd_env, "TEST_B=from_dotenv\n")
    _apply_env_sources(
        cwd_env_path=cwd_env,
        user_env_path=tmp_path / "nope.env",
        env_overrides=["TEST_B=from_flag"],
    )
    import os
    assert os.environ["TEST_B"] == "from_flag"


def test_env_flag_value_may_contain_equals(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_KV", raising=False)
    _apply_env_sources(
        cwd_env_path=tmp_path / "nope.env",
        user_env_path=tmp_path / "nope2.env",
        env_overrides=["TEST_KV=key=val=more"],
    )
    import os
    assert os.environ["TEST_KV"] == "key=val=more"


def test_malformed_env_flag_no_equals(tmp_path):
    with pytest.raises(ValueError) as exc:
        _apply_env_sources(
            cwd_env_path=tmp_path / "nope.env",
            user_env_path=tmp_path / "nope2.env",
            env_overrides=["MALFORMED"],
        )
    assert "KEY=VAL" in str(exc.value)
    assert "MALFORMED" in str(exc.value)


def test_malformed_env_flag_empty_key(tmp_path):
    with pytest.raises(ValueError) as exc:
        _apply_env_sources(
            cwd_env_path=tmp_path / "nope.env",
            user_env_path=tmp_path / "nope2.env",
            env_overrides=["=value"],
        )
    assert "key" in str(exc.value).lower()


def test_missing_env_files_is_ok(tmp_path):
    # Should not raise when neither .env file exists.
    _apply_env_sources(
        cwd_env_path=tmp_path / "missing.env",
        user_env_path=tmp_path / "also_missing.env",
        env_overrides=[],
    )

from __future__ import annotations

import os

import pytest

from morty_code.security.os_sandbox import (
    SandboxConfig,
    SandboxUnavailable,
    build_bash_argv,
    sandbox_metadata,
)


def test_build_bash_argv_wraps_command_with_bwrap(tmp_path) -> None:
    config = SandboxConfig(bwrap_path="/usr/bin/bwrap")

    argv = build_bash_argv("printf ok", root=tmp_path, env={}, config=config)

    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in argv
    assert "--bind" in argv
    assert str(tmp_path) in argv
    assert argv[-3:] == ["/bin/sh", "-lc", "printf ok"]


def test_build_bash_argv_can_disable_sandbox_explicitly(tmp_path) -> None:
    config = SandboxConfig(enabled=False, bwrap_path=None)

    argv = build_bash_argv("printf ok", root=tmp_path, env={}, config=config)

    assert argv == ["/bin/sh", "-lc", "printf ok"]


def test_build_bash_argv_fails_closed_when_bwrap_missing(tmp_path) -> None:
    config = SandboxConfig(bwrap_path=None)

    with pytest.raises(SandboxUnavailable, match="bwrap"):
        build_bash_argv("printf ok", root=tmp_path, env={}, config=config)


def test_build_bash_argv_allows_network_when_enabled(tmp_path) -> None:
    config = SandboxConfig(bwrap_path="/usr/bin/bwrap", allow_network=True)

    argv = build_bash_argv("printf ok", root=tmp_path, env={}, config=config)

    assert "--unshare-net" not in argv


def test_sandbox_metadata_reflects_env_flags(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MORTY_DISABLE_OS_SANDBOX", "1")
    monkeypatch.setenv("MORTY_SANDBOX_NETWORK", "1")

    metadata = sandbox_metadata(tmp_path, env=os.environ)

    assert metadata["enabled"] is False
    assert metadata["allow_network"] is True

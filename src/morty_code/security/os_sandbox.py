from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping


class SandboxUnavailable(RuntimeError):
    """OS sandbox is required but cannot be started."""


@dataclass(frozen=True)
class SandboxConfig:
    enabled: bool = True
    allow_network: bool = False
    network_isolation_requested: bool = True
    bwrap_path: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SandboxConfig":
        """从外部状态构建对象。"""
        values = env or os.environ
        enabled = values.get("MORTY_DISABLE_OS_SANDBOX") != "1"
        bwrap_path = shutil.which("bwrap", path=values.get("PATH"))
        requested_network = values.get("MORTY_SANDBOX_NETWORK") != "1"
        allow_network = not requested_network
        if enabled and requested_network and bwrap_path and not _can_unshare_network(bwrap_path):
            allow_network = True
        return cls(
            enabled=enabled,
            allow_network=allow_network,
            network_isolation_requested=requested_network,
            bwrap_path=bwrap_path,
        )


def build_bash_argv(
    command: str,
    *,
    root: Path,
    env: Mapping[str, str] | None = None,
    config: SandboxConfig | None = None,
) -> list[str]:
    """构建默认带 OS sandbox 的 bash 工具 argv。"""

    sandbox = config or SandboxConfig.from_env(env)
    if not sandbox.enabled:
        return ["/bin/sh", "-lc", command]
    if not sandbox.bwrap_path:
        raise SandboxUnavailable("bwrap is required for OS sandboxing")

    workspace = root.resolve()
    argv = [
        sandbox.bwrap_path,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
    ]
    if not sandbox.allow_network:
        argv.append("--unshare-net")
    argv.extend(_readonly_mount_args())
    argv.extend(_workspace_mount_args(workspace))
    argv.extend(
        [
            "--setenv",
            "PWD",
            str(workspace),
            "--chdir",
            str(workspace),
            "/bin/sh",
            "-lc",
            command,
        ]
    )
    return argv


def sandbox_metadata(root: Path, *, env: Mapping[str, str] | None = None) -> dict[str, object]:
    """处理该方法负责的业务逻辑。"""
    config = SandboxConfig.from_env(env)
    return {
        "enabled": config.enabled,
        "backend": "bwrap" if config.enabled else "disabled",
        "available": bool(config.bwrap_path),
        "bwrap_path": config.bwrap_path,
        "allow_network": config.allow_network,
        "network_isolation_requested": config.network_isolation_requested,
        "network_isolated": config.enabled and not config.allow_network,
        "workspace": str(root.resolve()),
    }


def _readonly_mount_args() -> list[str]:
    """内部读取持久化内容。"""
    args: list[str] = []
    for raw_path in ("/usr", "/bin", "/lib", "/lib64"):
        path = Path(raw_path)
        if path.exists():
            args.extend(["--ro-bind", raw_path, raw_path])
    return args


def _workspace_mount_args(workspace: Path) -> list[str]:
    """内部处理该方法负责的业务逻辑。"""
    args: list[str] = []
    tmp = Path("/tmp")
    if tmp.exists():
        args.extend(["--bind", "/tmp", "/tmp"])
    try:
        workspace.relative_to(tmp)
    except ValueError:
        args.extend(["--bind", str(workspace), str(workspace)])
    return args


@lru_cache(maxsize=4)
def _can_unshare_network(bwrap_path: str) -> bool:
    """内部处理该方法负责的业务逻辑。"""
    try:
        proc = subprocess.run(
            [
                bwrap_path,
                "--die-with-parent",
                "--unshare-user",
                "--unshare-net",
                "--ro-bind",
                "/usr",
                "/usr",
                "/bin/sh",
                "-lc",
                "true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0

from __future__ import annotations

import re
import shlex
from pathlib import Path


class SecurityViolation(PermissionError):
    """工具安全策略拒绝执行。"""


_SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".envrc",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_ed25519",
}

_SENSITIVE_DIR_NAMES = {
    ".git",
    ".claude",
    ".ssh",
}

_MORTY_INTERNAL_DIRS = {
    "sessions",
    "subagents",
    "tasks",
    "tool-results",
}

_DANGEROUS_COMMANDS = {
    "sudo",
    "su",
    "chmod",
    "chown",
    "mkfs",
    "mount",
    "umount",
    "shutdown",
    "reboot",
    "systemctl",
    "service",
}

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"`"), "backtick command substitution"),
    (re.compile(r"\$\("), "$() command substitution"),
    (re.compile(r"<\("), "process substitution <()"),
    (re.compile(r">\("), "process substitution >()"),
    (re.compile(r"\$\{"), "${} shell expansion"),
    (re.compile(r"\|\s*(sh|bash|zsh)\b"), "pipe to shell"),
    (re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sh|bash|zsh)\b"), "download and execute"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"\bgit\s+clean\b[^;&|]*-[a-zA-Z]*f"), "git clean force"),
    (re.compile(r"\bgit\s+checkout\s+(--\s+)?\."), "git checkout current tree"),
    (re.compile(r"\bgit\s+restore\s+(--\s+)?\."), "git restore current tree"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f"), "recursive force removal"),
    (re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r"), "recursive force removal"),
]


def assert_safe_read_path(root: Path, path: Path) -> None:
    _assert_under_root(root, path)


def assert_safe_write_path(root: Path, path: Path) -> None:
    _assert_under_root(root, path)
    lowered_parts = {part.lower() for part in path.parts}
    if lowered_parts & _SENSITIVE_DIR_NAMES:
        raise SecurityViolation(f"writing sensitive directory is blocked: {path}")
    if ".morty" in lowered_parts:
        parts = [part.lower() for part in path.parts]
        for index, part in enumerate(parts[:-1]):
            if part == ".morty" and parts[index + 1] in _MORTY_INTERNAL_DIRS:
                raise SecurityViolation(f"writing morty internal state is blocked: {path}")
    if path.name.lower() in _SENSITIVE_FILE_NAMES or path.name.lower().startswith(".env."):
        raise SecurityViolation(f"writing sensitive file is blocked: {path}")


def assert_safe_bash_command(
    command: str,
    *,
    root: Path,
    allow_dangerous: bool = False,
) -> None:
    if allow_dangerous:
        return
    for pattern, reason in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            raise SecurityViolation(f"blocked bash command: {reason}")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise SecurityViolation(f"blocked malformed shell command: {exc}") from exc
    for segment in _split_command_segments(tokens):
        if not segment:
            continue
        executable = Path(segment[0]).name
        if executable in _DANGEROUS_COMMANDS:
            raise SecurityViolation(f"blocked dangerous command: {executable}")
        if executable in {"rm", "rmdir"}:
            _check_removal(segment[1:], root)


def _assert_under_root(root: Path, path: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise SecurityViolation(f"path escapes workspace root: {path}") from exc


def _split_command_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {";", "&&", "||", "|"}:
            segments.append([])
            continue
        segments[-1].append(token)
    return segments


def _check_removal(args: list[str], root: Path) -> None:
    positional = [arg for arg in args if not arg.startswith("-")]
    if not positional:
        return
    dangerous_targets = {"/", ".", "..", "~", str(root), root.name}
    for raw in positional:
        cleaned = raw.strip("'\"")
        if cleaned in dangerous_targets or cleaned.endswith("/*"):
            raise SecurityViolation(f"blocked dangerous removal target: {raw}")

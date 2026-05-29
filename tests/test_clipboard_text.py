from __future__ import annotations

import subprocess

from morty_code.input.clipboard_text import ClipboardTextError, read_clipboard_text


def test_read_clipboard_text_uses_first_available_command(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name if name == "wl-paste" else None)

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="hello", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert read_clipboard_text() == "hello"
    assert calls == [["wl-paste", "--no-newline"]]


def test_read_clipboard_text_reports_missing_tools(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)

    try:
        read_clipboard_text(platform="linux")
    except ClipboardTextError as exc:
        assert "wl-paste" in str(exc)
    else:
        raise AssertionError("expected ClipboardTextError")

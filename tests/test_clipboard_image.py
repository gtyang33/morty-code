from __future__ import annotations

import subprocess

from morty_code.input.clipboard_image import read_clipboard_image


def test_read_clipboard_image_from_xclip_png(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name if name == "xclip" else None)

    def fake_run(command, **_kwargs):
        if command == ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-out"]:
            return subprocess.CompletedProcess(command, 0, stdout=b"image/png\ntext/plain\n", stderr=b"")
        if command == ["xclip", "-selection", "clipboard", "-t", "image/png", "-out"]:
            return subprocess.CompletedProcess(command, 0, stdout=b"PNG_BYTES", stderr=b"")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("subprocess.run", fake_run)

    image = read_clipboard_image(platform="linux")

    assert image == {
        "type": "image",
        "content": "UE5HX0JZVEVT",
        "media_type": "image/png",
        "filename": "clipboard.png",
    }


def test_read_clipboard_image_returns_none_when_no_image_target(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name if name == "xclip" else None)

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=b"text/plain\n", stderr=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert read_clipboard_image(platform="linux") is None

from __future__ import annotations

from pathlib import Path

import pytest

import video_context_mcp.doctor as doctor
from video_context_mcp.config import Settings
from video_context_mcp.doctor import Check, format_checks, required_checks_pass


def test_optional_check_does_not_fail_doctor() -> None:
    checks = [Check("required", True, "ok"), Check("optional", False, "missing", False)]
    assert required_checks_pass(checks)
    assert "[WARN] optional" in format_checks(checks)


def test_required_check_fails_doctor() -> None:
    checks = [Check("required", False, "missing")]
    assert not required_checks_pass(checks)
    assert "[FAIL] required" in format_checks(checks)


@pytest.mark.parametrize("version", [(3, 12), (3, 13), (3, 14)])
def test_supported_python_versions_pass(
    version: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor.sys, "version_info", (*version, 0, "final", 0))

    check = doctor._python_check()

    assert check.ok
    assert "supports 3.12-3.14" in check.detail


@pytest.mark.parametrize("version", [(3, 11), (3, 15)])
def test_unsupported_python_versions_fail(
    version: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor.sys, "version_info", (*version, 0, "final", 0))

    assert not doctor._python_check().ok


def test_doctor_honors_configured_native_executables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_check(name: str, executable: str, _args: list[str]) -> Check:
        calls.append((name, executable))
        detail = "tesseract 5.5.2" if name == "Tesseract" else "v22.1.0"
        return Check(name, True, detail)

    monkeypatch.setattr(doctor, "_executable_check", fake_check)
    settings = Settings(
        home=tmp_path / "home",
        allowed_roots=(tmp_path,),
        ffmpeg_executable="/tools/ffmpeg",
        ffprobe_executable="/tools/ffprobe",
        tesseract_executable="/tools/tesseract",
        node_executable="/tools/node",
    )

    checks = doctor.run_checks(settings)

    assert ("FFmpeg", "/tools/ffmpeg") in calls
    assert ("ffprobe", "/tools/ffprobe") in calls
    assert ("Tesseract", "/tools/tesseract") in calls
    assert ("Node.js", "/tools/node") in calls
    assert required_checks_pass(checks) is (doctor._python_check().ok)


def test_tesseract_4_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "_executable_check",
        lambda *_args, **_kwargs: Check("Tesseract", True, "tesseract 4.1.3"),
    )

    check = doctor._tesseract_check("tesseract")

    assert not check.ok
    assert "requires 5+" in check.detail

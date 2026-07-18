from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from video_context_mcp import __version__
from video_context_mcp.config import Settings


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def run_checks(settings: Settings | None = None) -> list[Check]:
    configured = settings or Settings.from_env()
    return [
        Check(
            "Keyframe",
            True,
            f"video-context-mcp {__version__} on Python {sys.version_info.major}.{sys.version_info.minor}",
        ),
        _python_check(),
        _executable_check("FFmpeg", configured.ffmpeg_executable, ["-version"]),
        _executable_check("ffprobe", configured.ffprobe_executable, ["-version"]),
        _tesseract_check(configured.tesseract_executable),
        _node_check(configured.node_executable),
        _module_check("yt-dlp", "yt_dlp"),
        _module_check("faster-whisper", "faster_whisper", required=False),
    ]


def required_checks_pass(checks: list[Check]) -> bool:
    return all(check.ok for check in checks if check.required)


def format_checks(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        marker = "PASS" if check.ok else ("WARN" if not check.required else "FAIL")
        lines.append(f"[{marker}] {check.name}: {check.detail}")
    return "\n".join(lines)


def _python_check() -> Check:
    version = sys.version_info[:2]
    ok = version == (3, 12)
    return Check("Python", ok, f"{version[0]}.{version[1]} (requires 3.12)")


def _executable_check(name: str, executable: str, args: list[str]) -> Check:
    path = shutil.which(executable)
    if path is None:
        return Check(name, False, f"{executable} was not found on PATH")
    try:
        result = subprocess.run(
            [path, *args],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(name, False, f"could not run {path}: {exc}")
    first_line = (result.stdout or result.stderr).splitlines()
    detail = first_line[0] if first_line else path
    return Check(name, result.returncode == 0, detail)


def _tesseract_check(executable: str) -> Check:
    check = _executable_check("Tesseract", executable, ["--version"])
    if not check.ok:
        return check
    match = re.search(r"(?:^|\s)(\d+)(?:\.\d+)+", check.detail)
    if match is None:
        return Check("Tesseract", False, f"could not parse version: {check.detail}")
    major = int(match.group(1))
    return Check("Tesseract", major >= 5, f"{check.detail} (requires 5+)")


def _node_check(executable: str = "node") -> Check:
    check = _executable_check("Node.js", executable, ["--version"])
    if not check.ok:
        return check
    try:
        major = int(check.detail.lstrip("v").split(".", maxsplit=1)[0])
    except ValueError:
        return Check("Node.js", False, f"could not parse version: {check.detail}")
    return Check("Node.js", major >= 22, f"{check.detail} (requires 22+ for yt-dlp EJS)")


def _module_check(name: str, module: str, *, required: bool = True) -> Check:
    available = importlib.util.find_spec(module) is not None
    if available:
        return Check(name, True, f"Python module {module} is installed", required)
    extra = " (install video-context-mcp[whisper])" if not required else ""
    return Check(name, False, f"Python module {module} is missing{extra}", required)

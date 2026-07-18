#!/usr/bin/env python3
"""Generate Keyframe's small, first-party synthetic MP4 fixture."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1280
HEIGHT = 720
FPS = 30
SCENE_DURATIONS = (3, 4, 3)
TITLE = "Keyframe First-Party Synthetic Fixture"


def _font_candidates() -> tuple[Path, ...]:
    configured = os.environ.get("KEYFRAME_FIXTURE_FONT")
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            Path("/System/Library/Fonts/SFNSMono.ttf"),
            Path("/System/Library/Fonts/Menlo.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf"),
            Path("C:/Windows/Fonts/consola.ttf"),
            Path("C:/Windows/Fonts/cour.ttf"),
        ]
    )
    return tuple(candidates)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _font_candidates():
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default(size=size)


def _centered_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    y: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
) -> None:
    bounds = draw.multiline_textbbox((0, 0), text, font=font, spacing=18, align="center")
    width = bounds[2] - bounds[0]
    draw.multiline_text(
        ((WIDTH - width) / 2, y),
        text,
        font=font,
        fill=fill,
        spacing=18,
        align="center",
    )


def _intro(path: Path) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#111827")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 20, HEIGHT), fill="#f59e0b")
    _centered_text(draw, "KEYFRAME", y=210, font=_font(82), fill="#f8fafc")
    _centered_text(
        draw,
        "LOCAL VIDEO RAG\nFIRST-PARTY SYNTHETIC FIXTURE",
        y=335,
        font=_font(35),
        fill="#93c5fd",
    )
    image.save(path, format="PNG", optimize=False)


def _code(path: Path) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#f8fafc")
    draw = ImageDraw.Draw(image)
    font = _font(42)
    lines = (
        ("import re", 110),
        ("def slugify_title(value: str) -> str:", 110),
        ("    clean = value.strip().lower()", 110),
        ('    return re.sub(r"[^a-z0-9]+", "-", clean).strip("-")', 110),
    )
    y = 160
    for text, x in lines:
        draw.text((x, y), text, font=font, fill="#0f172a")
        y += 92
    image.save(path, format="PNG", optimize=False)


def _terminal(path: Path) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#0b1220")
    draw = ImageDraw.Draw(image)
    font = _font(50)
    draw.text((150, 210), "$ pytest -q", font=font, fill="#a7f3d0")
    draw.text((150, 315), "4 passed in 0.03s", font=font, fill="#f8fafc")
    draw.text((150, 420), "$ echo verified", font=font, fill="#a7f3d0")
    image.save(path, format="PNG", optimize=False)


def _run_ffmpeg(ffmpeg: str, scenes: tuple[Path, Path, Path], output: Path) -> None:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    for duration, scene in zip(SCENE_DURATIONS, scenes, strict=True):
        command.extend(["-loop", "1", "-t", str(duration), "-i", str(scene)])
    command.extend(
        [
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[outv]",
            "-map",
            "[outv]",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "22",
            "-threads",
            "1",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-metadata",
            f"title={TITLE}",
            "-metadata",
            "creation_time=1970-01-01T00:00:00Z",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ]
    )
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"FFmpeg fixture generation failed:\n{completed.stderr.strip()}")


def generate(output: Path, *, ffmpeg: str, force: bool) -> None:
    if output.exists() and not force:
        raise FileExistsError(f"Fixture already exists: {output}; pass --force to replace it")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="keyframe-fixture-") as temp_dir:
        root = Path(temp_dir)
        scenes = (root / "intro.png", root / "code.png", root / "terminal.png")
        _intro(scenes[0])
        _code(scenes[1])
        _terminal(scenes[2])
        temporary_output = root / output.name
        _run_ffmpeg(ffmpeg, scenes, temporary_output)
        temporary_output.replace(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("keyframe-synthetic.mp4"),
    )
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()
    generate(arguments.output.resolve(), ffmpeg=arguments.ffmpeg, force=arguments.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Runtime configuration for Keyframe.

Configuration is deliberately environment-based so the same package works when it
is launched by an MCP client, from a shell, or inside a test harness.  Paths are
resolved eagerly: callers never need to reason about symlinks when enforcing the
local-source allowlist.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

from video_context_mcp.constants import (
    DEFAULT_MAX_DURATION_S,
    MAX_CONFIGURABLE_DURATION_S,
)
from video_context_mcp.errors import ConfigurationError

DEFAULT_MAX_LOCAL_FILE_BYTES = 20 * 1024**3
DEFAULT_MAX_REMOTE_FILE_BYTES = 10 * 1024**3
DEFAULT_MAX_SUBTITLE_BYTES = 20 * 1024**2


def _positive_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    maximum: int | None = None,
) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer, got {raw!r}.") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero, got {value}.")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} must not exceed {maximum}, got {value}.")
    return value


def _boolean(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(
        f"{name} must be one of true/false, 1/0, yes/no, or on/off; got {raw!r}."
    )


def _resolve_root(path: Path, *, setting: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError(f"{setting} directory does not exist: {path}") from exc
    if not resolved.is_dir():
        raise ConfigurationError(f"{setting} must name a directory: {resolved}")
    return resolved


def _ensure_private_temp_directory(path: Path, *, label: str) -> None:
    """Create and validate one Keyframe-owned directory below the OS temp root."""

    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        is_junction = getattr(path, "is_junction", None)
        if path.is_symlink() or (callable(is_junction) and is_junction()):
            raise ConfigurationError(f"{label} must not be a symbolic link or junction: {path}")
        metadata = path.lstat()
    except ConfigurationError:
        raise
    except OSError as exc:
        raise ConfigurationError(f"Could not create or inspect {label}: {path}") from exc

    if not stat.S_ISDIR(metadata.st_mode):
        raise ConfigurationError(f"{label} must be a directory: {path}")

    getuid = getattr(os, "getuid", None)
    if callable(getuid) and metadata.st_uid != getuid():
        raise ConfigurationError(
            f"{label} is not owned by the current user and cannot be trusted: {path}"
        )

    if os.name == "posix":
        try:
            path.chmod(0o700)
            hardened = path.lstat()
        except OSError as exc:
            raise ConfigurationError(f"Could not secure {label}: {path}") from exc
        if stat.S_IMODE(hardened.st_mode) != 0o700:
            raise ConfigurationError(f"{label} permissions could not be restricted: {path}")


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved Keyframe runtime settings.

    ``home`` is allowed not to exist until :meth:`ensure_directories` is called.
    By contrast, allowlisted source roots must already exist so a typo cannot
    silently broaden or change the local-file security boundary later.
    """

    home: Path
    allowed_roots: tuple[Path, ...]
    ffmpeg_executable: str = "ffmpeg"
    ffprobe_executable: str = "ffprobe"
    tesseract_executable: str = "tesseract"
    node_executable: str = "node"
    default_max_duration_s: int = DEFAULT_MAX_DURATION_S
    max_local_file_bytes: int = DEFAULT_MAX_LOCAL_FILE_BYTES
    max_remote_file_bytes: int = DEFAULT_MAX_REMOTE_FILE_BYTES
    max_subtitle_bytes: int = DEFAULT_MAX_SUBTITLE_BYTES
    allow_private_urls: bool = False
    allow_temp_uploads: bool = False

    @property
    def tmp_dir(self) -> Path:
        normalized_home = os.path.normcase(str(self.home.expanduser().resolve(strict=False)))
        namespace = hashlib.sha256(os.fsencode(normalized_home)).hexdigest()[:16]
        return Path(tempfile.gettempdir()).resolve(strict=False) / f"keyframe-{namespace}"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def upload_dir(self) -> Path:
        """Private cross-client staging directory for explicitly selected uploads."""

        return self.tmp_dir / "uploads"

    @property
    def authorized_local_roots(self) -> tuple[Path, ...]:
        """Return explicit roots plus the opt-in, Keyframe-owned upload staging root."""

        if not self.allow_temp_uploads or self.upload_dir in self.allowed_roots:
            return self.allowed_roots
        return (*self.allowed_roots, self.upload_dir)

    @property
    def artifacts_dir(self) -> Path:
        return self.home / "artifacts"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        cwd: Path | None = None,
    ) -> Settings:
        """Build settings from an environment mapping.

        Supported variables are ``KEYFRAME_HOME``, ``KEYFRAME_ALLOWED_ROOTS``
        (``os.pathsep`` separated), executable overrides, duration and byte caps,
        ``KEYFRAME_ALLOW_PRIVATE_URLS``, and ``KEYFRAME_ALLOW_TEMP_UPLOADS``. Local
        files are authorized only by explicit configured roots, per-request roots
        advertised by an MCP client, or the private upload staging directory when
        explicitly enabled; process CWD is never trusted implicitly.
        """

        values = os.environ if env is None else env
        del cwd  # Kept as a compatibility-only keyword; CWD is not an authorization boundary.

        configured_home = values.get("KEYFRAME_HOME", "").strip()
        if configured_home:
            home = Path(configured_home).expanduser().resolve(strict=False)
        else:
            home = Path(user_data_path("Keyframe", appauthor=False)).resolve(strict=False)

        roots: list[Path] = []
        raw_roots = values.get("KEYFRAME_ALLOWED_ROOTS", "")
        for raw_root in raw_roots.split(os.pathsep):
            if not raw_root.strip():
                continue
            root = _resolve_root(Path(raw_root.strip()), setting="KEYFRAME_ALLOWED_ROOTS")
            if root not in roots:
                roots.append(root)

        executable_names = {
            "ffmpeg_executable": ("KEYFRAME_FFMPEG", "ffmpeg"),
            "ffprobe_executable": ("KEYFRAME_FFPROBE", "ffprobe"),
            "tesseract_executable": ("KEYFRAME_TESSERACT", "tesseract"),
            "node_executable": ("KEYFRAME_NODE", "node"),
        }
        executables: dict[str, str] = {}
        for field, (name, default) in executable_names.items():
            value = values.get(name, default).strip()
            if not value:
                raise ConfigurationError(f"{name} must not be empty.")
            executables[field] = value

        return cls(
            home=home,
            allowed_roots=tuple(roots),
            default_max_duration_s=_positive_int(
                values,
                "KEYFRAME_MAX_DURATION_S",
                DEFAULT_MAX_DURATION_S,
                maximum=MAX_CONFIGURABLE_DURATION_S,
            ),
            max_local_file_bytes=_positive_int(
                values,
                "KEYFRAME_MAX_LOCAL_FILE_BYTES",
                DEFAULT_MAX_LOCAL_FILE_BYTES,
            ),
            max_remote_file_bytes=_positive_int(
                values,
                "KEYFRAME_MAX_REMOTE_FILE_BYTES",
                DEFAULT_MAX_REMOTE_FILE_BYTES,
            ),
            max_subtitle_bytes=_positive_int(
                values,
                "KEYFRAME_MAX_SUBTITLE_BYTES",
                DEFAULT_MAX_SUBTITLE_BYTES,
            ),
            allow_private_urls=_boolean(values, "KEYFRAME_ALLOW_PRIVATE_URLS"),
            allow_temp_uploads=_boolean(values, "KEYFRAME_ALLOW_TEMP_UPLOADS"),
            **executables,
        )

    def ensure_directories(self) -> None:
        """Create private runtime directories needed by acquisition and storage."""

        for directory in (self.home, self.cache_dir, self.artifacts_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _ensure_private_temp_directory(self.tmp_dir, label="Keyframe temp namespace")
        if self.allow_temp_uploads:
            _ensure_private_temp_directory(
                self.upload_dir,
                label="Keyframe upload staging root",
            )

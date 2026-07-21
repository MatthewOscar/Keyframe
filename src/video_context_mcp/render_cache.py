"""Private, bounded publication cache for images rendered inline by MCP hosts."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath

from filelock import FileLock, Timeout

from video_context_mcp.config import Settings, _ensure_private_temp_directory
from video_context_mcp.constants import MAX_IMAGE_BYTES
from video_context_mcp.errors import CacheError

DEFAULT_RENDER_CACHE_TTL_S = 7 * 24 * 60 * 60
DEFAULT_RENDER_CACHE_BYTES = 256 * 1024**2
_LOCK_TIMEOUT_S = 30
_MIME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
}
_FRAME_NAME_RE = re.compile(r"frame-([0-9a-f]{64})\.(?:jpg|png)\Z")


@dataclass(frozen=True, slots=True)
class RenderedFrame:
    """One exact image copy suitable for direct Markdown rendering."""

    path: Path
    markdown: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class RenderPruneResult:
    removed_files: int
    removed_bytes: int
    retained_files: int
    retained_bytes: int


def format_render_timestamp(timestamp_s: float) -> str:
    """Format a non-negative media timestamp for a concise Markdown alt label."""

    if not math.isfinite(timestamp_s) or timestamp_s < 0:
        raise CacheError("A rendered frame requires a finite non-negative timestamp.")
    total_seconds = math.floor(timestamp_s)
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def make_render_markdown(path: PurePath, timestamp_s: float) -> str:
    """Build CommonMark-safe local-image syntax, including Windows path normalization."""

    destination = path.as_posix()
    if any(character in destination for character in ("\n", "\r", "<", ">")):
        raise CacheError("The temporary render path contains characters unsafe for Markdown.")
    timestamp = format_render_timestamp(timestamp_s)
    return f"![Keyframe frame at {timestamp}](<{destination}>)"


class RenderedFrameCache:
    """Publish exact encoded image bytes under a private TTL- and quota-bounded root."""

    def __init__(
        self,
        settings: Settings,
        *,
        ttl_s: int = DEFAULT_RENDER_CACHE_TTL_S,
        quota_bytes: int = DEFAULT_RENDER_CACHE_BYTES,
    ) -> None:
        if ttl_s <= 0:
            raise CacheError("Rendered-frame cache TTL must be greater than zero.")
        if quota_bytes <= 0:
            raise CacheError("Rendered-frame cache quota must be greater than zero.")
        self.root = settings.rendered_frames_dir
        self.ttl_s = ttl_s
        self.quota_bytes = quota_bytes
        self._lock = FileLock(str(settings.tmp_dir / "rendered-frames.lock"))

    def publish(
        self,
        data: bytes,
        mime_type: str,
        *,
        timestamp_s: float,
    ) -> RenderedFrame:
        """Atomically publish and reuse a deterministic byte-identical image artifact."""

        if not data:
            raise CacheError("Cannot publish an empty rendered frame.")
        if len(data) > MAX_IMAGE_BYTES:
            raise CacheError(
                f"Rendered frame is {len(data)} bytes, above the {MAX_IMAGE_BYTES}-byte image limit."
            )
        if len(data) > self.quota_bytes:
            raise CacheError(
                f"Rendered frame is {len(data)} bytes, above the {self.quota_bytes}-byte cache quota."
            )
        try:
            suffix = _MIME_SUFFIXES[mime_type]
        except KeyError as exc:
            raise CacheError(f"Unsupported rendered-frame MIME type: {mime_type!r}.") from exc

        digest = hashlib.sha256(data).hexdigest()
        candidate = self.root / f"frame-{digest}{suffix}"
        now = time.time()
        try:
            with self._lock.acquire(timeout=_LOCK_TIMEOUT_S):
                self._ensure_root()
                if not self._is_reusable(candidate, data, now=now):
                    self._replace(candidate, data, mtime=now)
                self._secure_regular_file(candidate)
                self._prune_locked(
                    now=now,
                    protected=candidate,
                    validate_content=False,
                )
                metadata = candidate.lstat()
        except Timeout as exc:
            raise CacheError("Timed out waiting to publish the temporary rendered frame.") from exc
        except CacheError:
            raise
        except OSError as exc:
            raise CacheError(f"Could not publish the temporary rendered frame: {exc}") from exc

        return RenderedFrame(
            path=candidate.absolute(),
            markdown=make_render_markdown(candidate.absolute(), timestamp_s),
            expires_at=datetime.fromtimestamp(metadata.st_mtime + self.ttl_s, tz=UTC).isoformat(),
        )

    def prune(self, *, create: bool = True) -> RenderPruneResult:
        """Remove unsafe, interrupted, expired, and over-quota cache entries."""

        if not create and not self.root.exists():
            return RenderPruneResult(0, 0, 0, 0)
        try:
            with self._lock.acquire(timeout=_LOCK_TIMEOUT_S):
                self._ensure_root()
                return self._prune_locked(
                    now=time.time(),
                    protected=None,
                    validate_content=True,
                )
        except Timeout as exc:
            raise CacheError("Timed out waiting to prune temporary rendered frames.") from exc
        except CacheError:
            raise
        except OSError as exc:
            raise CacheError(f"Could not prune temporary rendered frames: {exc}") from exc

    def _ensure_root(self) -> None:
        _ensure_private_temp_directory(
            self.root,
            label="Keyframe rendered-frame cache",
        )

    def _is_reusable(self, candidate: Path, expected: bytes, *, now: float) -> bool:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return False
        if _is_link_or_junction(candidate) or not stat.S_ISREG(metadata.st_mode):
            _remove_without_following(candidate)
            return False
        if metadata.st_size != len(expected):
            candidate.unlink(missing_ok=True)
            return False
        if now - metadata.st_mtime >= self.ttl_s:
            candidate.unlink(missing_ok=True)
            return False
        try:
            matches = candidate.read_bytes() == expected
        except OSError:
            matches = False
        if not matches:
            candidate.unlink(missing_ok=True)
        return matches

    def _replace(self, candidate: Path, data: bytes, *, mtime: float) -> None:
        staged = self.root / f".staging-{uuid.uuid4().hex}{candidate.suffix}"
        descriptor: int | None = None
        try:
            descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            staged.chmod(0o600)
            os.replace(staged, candidate)
            os.utime(candidate, (mtime, mtime))
        finally:
            if descriptor is not None:
                os.close(descriptor)
            staged.unlink(missing_ok=True)

    def _secure_regular_file(self, candidate: Path) -> None:
        metadata = candidate.lstat()
        if _is_link_or_junction(candidate) or not stat.S_ISREG(metadata.st_mode):
            raise CacheError(f"Rendered-frame cache entry is not a regular file: {candidate}")
        if os.name == "posix":
            candidate.chmod(0o600)
            if stat.S_IMODE(candidate.lstat().st_mode) != 0o600:
                raise CacheError(f"Rendered-frame permissions could not be restricted: {candidate}")

    def _prune_locked(
        self,
        *,
        now: float,
        protected: Path | None,
        validate_content: bool,
    ) -> RenderPruneResult:
        removed_files = 0
        removed_bytes = 0
        retained: list[tuple[float, int, Path]] = []

        for entry in tuple(self.root.iterdir()):
            try:
                metadata = entry.lstat()
            except FileNotFoundError:
                continue
            size = metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0
            unsafe = _is_link_or_junction(entry) or not stat.S_ISREG(metadata.st_mode)
            interrupted = entry.name.startswith(".staging-")
            name_match = _FRAME_NAME_RE.fullmatch(entry.name)
            unknown = name_match is None
            corrupt = False
            if validate_content and not unsafe and not interrupted and name_match is not None:
                corrupt = metadata.st_size > MAX_IMAGE_BYTES
                if not corrupt:
                    try:
                        corrupt = _file_digest(entry) != name_match.group(1)
                    except OSError:
                        corrupt = True
            expired = now - metadata.st_mtime >= self.ttl_s and entry != protected
            if unsafe or interrupted or unknown or corrupt or expired:
                _remove_without_following(entry)
                removed_files += 1
                removed_bytes += size
                continue
            retained.append((metadata.st_mtime, size, entry))

        retained_bytes = sum(size for _mtime, size, _entry in retained)
        for _mtime, size, entry in sorted(retained):
            if retained_bytes <= self.quota_bytes:
                break
            if entry == protected:
                continue
            entry.unlink(missing_ok=True)
            retained_bytes -= size
            removed_files += 1
            removed_bytes += size

        retained_files = sum(1 for _mtime, _size, entry in retained if entry.exists())
        return RenderPruneResult(
            removed_files=removed_files,
            removed_bytes=removed_bytes,
            retained_files=retained_files,
            retained_bytes=retained_bytes,
        )


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (callable(is_junction) and is_junction())


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_without_following(path: Path) -> None:
    """Remove one cache entry without recursively traversing an untrusted target."""

    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        path.rmdir()
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    try:
        path.rmdir()
    except OSError as exc:
        raise CacheError(f"Refusing to traverse unexpected render-cache directory: {path}") from exc

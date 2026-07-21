"""Bounded local cache for seekable, low-resolution remote-video proxies."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from video_context_mcp.config import Settings
from video_context_mcp.constants import SUPPORTED_VIDEO_EXTENSIONS
from video_context_mcp.errors import CacheError

_SAFE_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
_REMUX_TIMEOUT_S = 300


def _set_regular_file_mtime(path: Path, timestamp: float) -> None:
    """Update a regular file without relying on unsupported Windows arguments."""

    times = (timestamp, timestamp)
    if os.utime in os.supports_follow_symlinks:
        os.utime(path, times, follow_symlinks=False)
        return

    # Windows does not implement ``follow_symlinks`` for os.utime. Re-check the
    # file immediately before using its portable form so a cache link is never
    # accepted intentionally on platforms without that safeguard.
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"Refusing to update a non-regular proxy-cache file: {path}")
    os.utime(path, times)


@dataclass(frozen=True, slots=True)
class ProxyInfo:
    path: Path
    size_bytes: int
    expires_at: str


@dataclass(frozen=True, slots=True)
class ProxyPruneResult:
    removed_files: int
    removed_bytes: int
    retained_files: int
    retained_bytes: int


class ProxyCache:
    """Retain disposable probe media under a TTL and global LRU byte quota."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        configured_root = settings.proxy_dir
        if configured_root.is_symlink():
            raise CacheError(
                f"Proxy-cache root must not be a symbolic link: {configured_root}"
            )
        self.root = configured_root.resolve(strict=False)

    @property
    def enabled(self) -> bool:
        return self.settings.proxy_cache_ttl_s > 0 and self.settings.proxy_cache_bytes > 0

    def get(self, video_id: str, *, touch: bool = False) -> ProxyInfo | None:
        entry = self._entry(video_id)
        try:
            candidate = self._media_file(entry)
        except OSError as exc:
            raise CacheError(f"Could not inspect proxy-cache entry: {entry}") from exc
        if candidate is None:
            return None
        try:
            metadata = candidate.lstat()
        except OSError:
            return None
        if candidate.is_symlink() or not candidate.is_file():
            self._remove_entry(entry)
            return None
        now = time.time()
        if not self.enabled or now - metadata.st_mtime > self.settings.proxy_cache_ttl_s:
            self._remove_entry(entry)
            return None
        if touch:
            try:
                _set_regular_file_mtime(candidate, now)
                metadata = candidate.stat()
            except OSError as exc:
                raise CacheError(f"Could not update proxy-cache access time: {candidate}") from exc
        expires = datetime.fromtimestamp(
            metadata.st_mtime + self.settings.proxy_cache_ttl_s,
            tz=UTC,
        ).isoformat()
        return ProxyInfo(candidate, metadata.st_size, expires)

    def promote(
        self,
        video_id: str,
        media_path: Path,
        *,
        contains_audio: bool,
        ffmpeg_binary: str,
    ) -> ProxyInfo | None:
        """Atomically retain owned probe media, stripping audio when necessary."""

        if not self.enabled:
            return None
        try:
            media_is_file = media_path.is_file()
            source_size = media_path.stat().st_size if media_is_file else 0
        except OSError as exc:
            raise CacheError(f"Could not inspect downloaded probe media: {media_path}") from exc
        if not media_is_file:
            raise CacheError(f"Probe media disappeared before proxy publication: {media_path}")
        if source_size > self.settings.proxy_cache_bytes:
            return None

        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            entry = self._entry(video_id)
            entry.mkdir(mode=0o700, parents=False, exist_ok=True)
        except OSError as exc:
            raise CacheError(f"Could not prepare the proxy-cache entry for {video_id!r}.") from exc
        if entry.is_symlink():
            raise CacheError(f"Proxy-cache entry must not be a symbolic link: {entry}")

        token = uuid.uuid4().hex
        if contains_audio:
            staged = entry / f".staging-{token}.mkv"
            command = [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(media_path),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-c:v",
                "copy",
                "-y",
                str(staged),
            ]
            try:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=_REMUX_TIMEOUT_S,
                )
            except FileNotFoundError as exc:
                staged.unlink(missing_ok=True)
                raise CacheError(
                    f"FFmpeg executable {ffmpeg_binary!r} was not found while caching a proxy."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                staged.unlink(missing_ok=True)
                raise CacheError("Timed out while stripping audio from the cached proxy.") from exc
            except OSError as exc:
                staged.unlink(missing_ok=True)
                raise CacheError(f"Could not create a silent cached proxy: {exc}") from exc
            if completed.returncode != 0 or not staged.is_file():
                staged.unlink(missing_ok=True)
                detail = completed.stderr.strip() or "no diagnostic output"
                raise CacheError(f"Could not create a silent cached proxy: {detail[-1_000:]}")
        else:
            suffix = media_path.suffix.lower()
            if suffix not in SUPPORTED_VIDEO_EXTENSIONS:
                suffix = ".mp4"
            staged = entry / f".staging-{token}{suffix}"
            try:
                shutil.move(str(media_path), staged)
            except OSError as exc:
                staged.unlink(missing_ok=True)
                raise CacheError(f"Could not retain the downloaded visual proxy: {exc}") from exc

        try:
            staged.chmod(0o600)
            final = entry / f"media{staged.suffix.lower()}"
            for old in entry.glob("media.*"):
                if old != final and old.is_file() and not old.is_symlink():
                    old.unlink(missing_ok=True)
            os.replace(staged, final)
            now = time.time()
            _set_regular_file_mtime(final, now)
        except OSError as exc:
            raise CacheError(f"Could not publish the bounded proxy for {video_id!r}.") from exc
        finally:
            staged.unlink(missing_ok=True)

        self.prune()
        return self.get(video_id)

    def prune(self) -> ProxyPruneResult:
        """Remove expired entries, then evict least-recently-used proxies to quota."""

        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            entries = tuple(self.root.iterdir())
        except OSError as exc:
            raise CacheError(f"Could not inspect proxy-cache root: {self.root}") from exc
        now = time.time()
        removed_files = 0
        removed_bytes = 0
        retained: list[tuple[float, int, Path]] = []
        for entry in entries:
            if entry.is_symlink():
                self._remove_entry(entry)
                continue
            if not entry.is_dir():
                continue
            try:
                candidate = self._media_file(entry)
            except OSError as exc:
                raise CacheError(f"Could not inspect proxy-cache entry: {entry}") from exc
            if candidate is None:
                self._remove_entry(entry)
                continue
            try:
                metadata = candidate.lstat()
            except OSError:
                self._remove_entry(entry)
                continue
            expired = (
                not self.enabled
                or candidate.is_symlink()
                or not candidate.is_file()
                or now - metadata.st_mtime > self.settings.proxy_cache_ttl_s
            )
            if expired:
                removed_files += 1
                removed_bytes += metadata.st_size
                self._remove_entry(entry)
            else:
                retained.append((metadata.st_mtime, metadata.st_size, entry))

        retained_bytes = sum(size for _, size, _ in retained)
        retained_files = len(retained)
        for _, size, entry in sorted(retained):
            if retained_bytes <= self.settings.proxy_cache_bytes:
                break
            self._remove_entry(entry)
            retained_bytes -= size
            retained_files -= 1
            removed_files += 1
            removed_bytes += size
        return ProxyPruneResult(
            removed_files=removed_files,
            removed_bytes=removed_bytes,
            retained_files=retained_files,
            retained_bytes=retained_bytes,
        )

    def _entry(self, video_id: str) -> Path:
        if _SAFE_VIDEO_ID_RE.fullmatch(video_id) is None:
            raise CacheError(f"Unsafe video ID for proxy cache: {video_id!r}")
        entry = self.root / video_id
        if entry.is_symlink():
            raise CacheError(f"Proxy-cache entry must not be a symbolic link: {entry}")
        if entry.parent.resolve(strict=False) != self.root:
            raise CacheError("Proxy-cache path escaped its configured root.")
        return entry

    @staticmethod
    def _media_file(entry: Path) -> Path | None:
        if not entry.is_dir() or entry.is_symlink():
            return None
        candidates = [
            path
            for path in entry.glob("media.*")
            if path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
            and not path.is_symlink()
            and path.is_file()
        ]
        return max(candidates, key=lambda path: path.lstat().st_mtime, default=None)

    def _remove_entry(self, entry: Path) -> None:
        if entry.parent.resolve(strict=False) != self.root or entry == self.root:
            raise CacheError(f"Refusing to remove unsafe proxy-cache path: {entry}")
        if entry.is_symlink():
            try:
                entry.unlink(missing_ok=True)
            except OSError as exc:
                raise CacheError(f"Could not remove proxy-cache link: {entry}") from exc
        elif entry.is_dir():
            try:
                shutil.rmtree(entry)
            except OSError as exc:
                raise CacheError(f"Could not remove proxy-cache entry: {entry}") from exc
        else:
            try:
                entry.unlink(missing_ok=True)
            except OSError as exc:
                raise CacheError(f"Could not remove proxy-cache entry: {entry}") from exc

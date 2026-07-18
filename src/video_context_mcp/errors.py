from __future__ import annotations


class KeyframeError(Exception):
    """Base class for actionable Keyframe failures."""


class ConfigurationError(KeyframeError):
    """The local runtime is missing required configuration or executables."""


class SourceError(KeyframeError):
    """A video source is invalid, unsupported, or unavailable."""


class CacheError(KeyframeError):
    """Cached state is unavailable or inconsistent."""


class ExtractionError(KeyframeError):
    """Video extraction failed before an atomic cache commit."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys

from video_context_mcp import __version__
from video_context_mcp.config import Settings
from video_context_mcp.doctor import format_checks, required_checks_pass, run_checks
from video_context_mcp.errors import KeyframeError
from video_context_mcp.proxy_cache import ProxyCache
from video_context_mcp.server import create_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-context-mcp",
        description="Keyframe: local video context for Codex and ChatGPT Desktop",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "doctor", "version", "cache"),
        default="serve",
    )
    parser.add_argument(
        "cache_command",
        nargs="?",
        choices=("prune",),
        help="Cache maintenance action; currently only `cache prune` is supported.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport; HTTP is a localhost-only development option.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cache_command is not None and args.command != "cache":
        raise SystemExit("the `prune` action may only be used with the cache command")
    if args.command == "doctor":
        checks = run_checks()
        print(format_checks(checks))
        raise SystemExit(0 if required_checks_pass(checks) else 1)
    if args.command == "version":
        print(__version__)
        return
    if args.command == "cache":
        if args.cache_command != "prune":
            raise SystemExit("cache requires the `prune` action")
        try:
            settings = Settings.from_env()
            settings.ensure_directories()
            result = ProxyCache(settings).prune()
        except KeyframeError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            "Proxy cache pruned: "
            f"removed {result.removed_files} file(s) ({result.removed_bytes} bytes); "
            f"retained {result.retained_files} file(s) ({result.retained_bytes} bytes)."
        )
        return
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.transport == "streamable-http" and not _is_loopback(args.host):
        raise SystemExit("The development HTTP transport may bind only to localhost.")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = create_server(host=args.host, port=args.port)
    server.run(transport=args.transport)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    main()

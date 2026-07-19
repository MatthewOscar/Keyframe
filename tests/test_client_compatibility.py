from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "keyframe"
RELEASE_SOURCE = "git+https://github.com/MatthewOscar/Keyframe.git@v0.1.0"
SERVER_TAIL = ["video-context-mcp", "serve", "--transport", "stdio"]
RELEASE_ARGS = ["--python", "3.12", "--from", RELEASE_SOURCE, *SERVER_TAIL]


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _server(path: Path) -> dict[str, Any]:
    value = _load(path)["mcpServers"]["keyframe"]
    assert isinstance(value, dict)
    return value


def _assert_release_launcher(server: dict[str, Any]) -> None:
    assert server["command"] == "uvx"
    assert server["args"] == RELEASE_ARGS


def test_project_configs_use_each_clients_documented_discovery_schema() -> None:
    claude = _server(ROOT / ".mcp.json")
    assert claude == {
        "type": "stdio",
        "command": "uv",
        "args": [
            "run",
            "--project",
            "${CLAUDE_PROJECT_DIR:-.}",
            *SERVER_TAIL,
        ],
        "timeout": 1_900_000,
    }

    cursor = _server(ROOT / ".cursor" / "mcp.json")
    assert cursor == {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "--project", ".", *SERVER_TAIL],
    }

    agy = _server(ROOT / ".agents" / "mcp_config.json")
    assert agy == {
        "command": "uv",
        "args": ["run", "--project", ".", *SERVER_TAIL],
        "cwd": ".",
    }


def test_plugin_manifests_reference_client_specific_mcp_configs() -> None:
    expected = {
        ".codex-plugin/plugin.json": "./.mcp.json",
        ".claude-plugin/plugin.json": "./claude.mcp.json",
        ".cursor-plugin/plugin.json": "./mcp.json",
    }
    for manifest_name, relative_config in expected.items():
        manifest = _load(PLUGIN / manifest_name)
        assert manifest["name"] == "keyframe"
        assert manifest["skills"] == "./skills/"
        assert manifest["mcpServers"] == relative_config
        assert (PLUGIN / relative_config).is_file()

    codex = _load(PLUGIN / ".codex-plugin" / "plugin.json")
    claude = _load(PLUGIN / ".claude-plugin" / "plugin.json")
    cursor = _load(PLUGIN / ".cursor-plugin" / "plugin.json")
    assert set(codex) == {
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "skills",
        "mcpServers",
        "interface",
    }
    assert set(claude) == {
        "name",
        "displayName",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "skills",
        "mcpServers",
    }
    assert set(cursor) == {
        "name",
        "displayName",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "logo",
        "keywords",
        "category",
        "tags",
        "skills",
        "mcpServers",
    }

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = project["project"]["version"]
    assert {codex["version"], claude["version"], cursor["version"]} == {package_version}

    assert _load(PLUGIN / "plugin.json") == {
        "name": "keyframe",
        "description": (
            "Index videos locally and retrieve timestamped transcript, OCR, code, and "
            "frames through MCP."
        ),
    }


def test_plugin_launchers_pin_the_release_without_cross_client_timeout_fields() -> None:
    codex = _server(PLUGIN / ".mcp.json")
    claude = _server(PLUGIN / "claude.mcp.json")
    cursor = _server(PLUGIN / "mcp.json")
    agy = _server(PLUGIN / "mcp_config.json")

    for server in (codex, claude, cursor, agy):
        _assert_release_launcher(server)

    assert codex["startup_timeout_sec"] == 180
    assert codex["tool_timeout_sec"] == 1900
    assert claude["timeout"] == 1_900_000
    assert "startup_timeout_sec" not in claude
    assert "tool_timeout_sec" not in claude

    for server in (cursor, agy):
        assert set(server) <= {"type", "command", "args", "env", "cwd"}
        assert not {"timeout", "startup_timeout_sec", "tool_timeout_sec"} & set(server)


def test_marketplaces_point_to_the_same_self_contained_plugin() -> None:
    claude = _load(ROOT / ".claude-plugin" / "marketplace.json")
    cursor = _load(ROOT / ".cursor-plugin" / "marketplace.json")
    codex = _load(ROOT / ".agents" / "plugins" / "marketplace.json")

    assert claude["plugins"][0]["source"] == "./plugins/keyframe"
    assert cursor["metadata"]["pluginRoot"] == "plugins"
    assert cursor["plugins"][0]["source"] == "keyframe"
    assert codex["plugins"][0]["source"]["path"] == "./plugins/keyframe"
    assert all(catalog["plugins"][0]["name"] == "keyframe" for catalog in (claude, cursor, codex))
    assert set(claude) == {"name", "owner", "description", "plugins"}
    assert set(cursor) == {"name", "owner", "metadata", "plugins"}
    assert set(codex) == {"name", "interface", "plugins"}
    assert (ROOT / claude["plugins"][0]["source"]).resolve() == PLUGIN.resolve()
    assert (
        ROOT / cursor["metadata"]["pluginRoot"] / cursor["plugins"][0]["source"]
    ).resolve() == PLUGIN.resolve()
    assert (ROOT / codex["plugins"][0]["source"]["path"]).resolve() == PLUGIN.resolve()


def test_project_and_plugin_workflow_skills_stay_identical_and_client_neutral() -> None:
    paths = [
        PLUGIN / "skills" / "keyframe-video-rag" / "SKILL.md",
        ROOT / ".agents" / "skills" / "keyframe-video-rag" / "SKILL.md",
        ROOT / ".claude" / "skills" / "keyframe-video-rag" / "SKILL.md",
    ]
    contents = [path.read_text(encoding="utf-8") for path in paths]
    assert contents[0] == contents[1] == contents[2]
    assert "Use when a coding agent must understand" in contents[0]
    assert "Use when Codex must understand" not in contents[0]

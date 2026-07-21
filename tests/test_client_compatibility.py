from __future__ import annotations

import json
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "keyframe"
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PACKAGE_VERSION = PROJECT["project"]["version"]
RELEASE_SOURCE = f"video-context-mcp[whisper]=={PACKAGE_VERSION}"
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


def test_source_distribution_allowlist_excludes_private_local_evaluations() -> None:
    sdist = PROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert sdist["only-include"] == ["src", "tests"]
    ignore_patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/local-evals/" in ignore_patterns


def test_package_supports_python_312_through_314_with_a_312_language_floor() -> None:
    project = PROJECT["project"]
    assert project["requires-python"] == ">=3.12,<3.15"
    assert {
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
    } <= set(project["classifiers"])
    assert PROJECT["tool"]["ruff"]["target-version"] == "py312"
    assert PROJECT["tool"]["mypy"]["python_version"] == "3.12"
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"


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
        "env": {"KEYFRAME_ALLOW_TEMP_UPLOADS": "true"},
        "timeout": 1_900_000,
    }

    cursor = _server(ROOT / ".cursor" / "mcp.json")
    assert cursor == {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "--project", ".", *SERVER_TAIL],
        "env": {"KEYFRAME_ALLOW_TEMP_UPLOADS": "true"},
    }

    agy = _server(ROOT / ".agents" / "mcp_config.json")
    assert agy == {
        "command": "uv",
        "args": ["run", "--project", ".", *SERVER_TAIL],
        "cwd": ".",
        "env": {"KEYFRAME_ALLOW_TEMP_UPLOADS": "true"},
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

    assert {codex["version"], claude["version"], cursor["version"]} == {PACKAGE_VERSION}
    assert PROJECT["project"]["authors"] == [{"name": "Matthew Wyatt"}]
    assert codex["author"]["name"] == "Matthew Wyatt"
    assert codex["interface"]["developerName"] == "Matthew Wyatt"
    assert codex["interface"]["shortDescription"] == "Search what videos say and GIFs show"
    discovery_prompt = codex["interface"]["defaultPrompt"][0]
    assert len(codex["interface"]["defaultPrompt"]) == 3
    assert "strongly relevant public video about this topic" in discovery_prompt
    assert "analyze the best match with Keyframe" in discovery_prompt
    assert claude["author"]["name"] == "Matthew Wyatt"
    assert cursor["author"]["name"] == "Matthew Wyatt"

    icon_path = PLUGIN / codex["interface"]["composerIcon"]
    assert codex["interface"]["logo"] == codex["interface"]["composerIcon"]
    assert codex["interface"]["logoDark"] == codex["interface"]["composerIcon"]
    assert icon_path.is_file()
    view_box = ET.parse(icon_path).getroot().attrib["viewBox"].split()
    assert view_box[2] == view_box[3]

    assert _load(PLUGIN / "plugin.json") == {
        "name": "keyframe",
        "description": (
            "Analyze supplied or host-discovered videos and animated GIFs locally, then "
            "retrieve timestamped transcript, OCR, code, and frames through MCP."
        ),
    }


def test_plugin_launchers_pin_the_release_without_cross_client_timeout_fields() -> None:
    codex = _server(PLUGIN / ".mcp.json")
    claude = _server(PLUGIN / "claude.mcp.json")
    cursor = _server(PLUGIN / "mcp.json")
    agy = _server(PLUGIN / "mcp_config.json")

    for server in (codex, claude, cursor, agy):
        _assert_release_launcher(server)
        assert server["env"] == {"KEYFRAME_ALLOW_TEMP_UPLOADS": "true"}

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
    assert {claude["name"], cursor["name"], codex["name"]} == {"keyframe-tools"}
    assert all(
        catalog["name"] != catalog["plugins"][0]["name"] for catalog in (claude, cursor, codex)
    )
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
    normalized_skill = " ".join(contents[0].split())
    assert contents[0] == contents[1] == contents[2]
    assert contents[0].startswith("---\nname: keyframe-video-rag\ndescription: Use Keyframe")
    assert "A Keyframe invocation selects the analysis capability" in contents[0]
    assert "must not change the ordinary meaning of the user's topic" in contents[0]
    assert "Discover a source without changing the topic" in contents[0]
    assert '"build my own processor"\n   means a CPU' in contents[0]
    assert "use the host's normal web search" in contents[0]
    assert "at most three individual public" in contents[0]
    assert "direct watch URL for one public video" in contents[0]
    assert "product landing page is not an ingest candidate" in contents[0]
    assert '`video_search` without a `video_id` searches only' in contents[0]
    assert "Keyword overlap, a passing mention, or an adjacent technology" in contents[0]
    assert "duration-guard retry may repeat the same source" in contents[0]
    assert "ingest call for a second URL" in contents[0]
    assert "never auto-ingest an adjacent fallback" in contents[0]
    assert (
        "When every direct-video candidate is weak or adjacent, do not ingest"
        in normalized_skill
    )
    assert "ask the user to provide one" in normalized_skill
    assert "Attribute web discovery separately" in normalized_skill
    assert "requests must not open this skill" in contents[0]
    assert "call Keyframe MCP directly and never use browser or shell tools" in contents[0]
    assert "Use Keyframe for multi-evidence video or animated-GIF analysis" in contents[0]
    assert "explicitly invoked requests to find and analyze videos about a topic" in contents[0]
    assert "Show or share one frame: overriding fast path" in contents[0]
    assert "Do not call `video_list_moments`, `video_get_transcript`, or" in contents[0]
    assert "Call `video_get_frame` exactly once" in contents[0]
    assert "progress update may state the requested retrieval goal" in contents[0]
    assert "must not claim that an uninspected candidate" in contents[0]
    assert "requested quality" in contents[0]
    assert "entire final response and stop" in contents[0]
    assert "Add no prefix, suffix, bullet, timestamp/provenance line" in contents[0]
    assert "Use when Codex must understand" not in contents[0]
    assert "For each source, make at most one successful fast ingest" in contents[0]
    assert "Do not split, restage, or reconstruct the source" in contents[0]
    assert "Skip generic search for a whole-video summary" in contents[0]
    assert 'Map explicit requests for "quick," "fast," "overview," or "gist"' in contents[0]
    assert "generic whole-video summary over 30 minutes" in contents[0]
    assert "use exactly one routing" in contents[0]
    assert '`view="compact"`' in contents[0]
    assert "`limit=200`" in contents[0]
    assert "`proxy_cached=false`" in contents[0]
    assert "`refresh=true`" in contents[0]
    assert "at most six transcript windows" in contents[0]
    assert "each no longer than 90 seconds" in contents[0]
    assert "at most two consequential probe" in contents[0]
    assert "Do not full-upgrade merely because" in contents[0]
    assert '`quality="auto"`' in contents[0]
    assert "evidence_quality" in contents[0]
    assert "one targeted seek cannot settle" in contents[0]
    assert "Do not fan transcript pages or windows out to multiple agents" in contents[0]
    assert "count Keyframe calls" in contents[0]
    assert "copy the" in contents[0]
    assert "`render_markdown` byte-for-byte" in contents[0]
    assert "including its `<` and `>` destination delimiters" in contents[0]
    assert "not open a browser, use terminal or shell tools" in contents[0]
    assert "Never retrieve the same `moment_id`" in contents[0]
    assert 'never request `quality="source"` for a remote video' in contents[0]
    assert 'use `region="full"`, never' in contents[0]
    assert "Align a whole-object frame to the demonstrated action" in contents[0]
    assert "announcement timestamp as visual proof" in contents[0]
    assert "`video_get_frame` is the only visual retrieval tool" in contents[0]
    assert "Use `context` to distinguish an action" in contents[0]
    assert "prohibitions apply at every phase" in contents[0]
    assert "never test a source URL or path in downstream tools" in contents[0]
    assert "exactly one frame call" in contents[0]
    assert "without image input cannot evaluate candidates" in contents[0]
    assert "It must not\n   judge visual quality or infer components" in contents[0]
    assert "`render_markdown` the entire final response; add no other text" in contents[0]
    assert (
        "When this skill applies, open only through the exact host-provided locator" in contents[0]
    )
    assert "marketplace/keyframe/version/skills/keyframe-video-rag/SKILL.md" in contents[0]
    assert "do not search for another copy" in contents[0]
    assert "Copy the returned structured `video_id` byte-for-byte" in contents[0]
    assert "never wait for a" in contents[0]
    assert "follow-up such as" in contents[0]
    assert "Pass that episode's `start_s`/`end_s`" in contents[0]
    assert "never join an ID or" in contents[0]
    assert "`requested_t_covered`" in contents[0]
    assert "`Tesseract OCR:`" in contents[0]
    assert "temporally local evidence" in contents[0]
    verify_visuals = contents[0].split("## Verify visuals", maxsplit=1)[1]
    exact_selector_exception = (
        "If the user supplied an exact timestamp or `moment_id`, preserve that selector and "
        "skip search."
    )
    strict_search_sequence = "For a no-vision show/share request about an action"
    assert exact_selector_exception in verify_visuals
    assert verify_visuals.index(exact_selector_exception) < verify_visuals.index(
        strict_search_sequence
    )
    assert "sole requested deliverable is one image" in contents[0]
    server_source = (ROOT / "src/video_context_mcp/server.py").read_text(encoding="utf-8")
    assert "Ingest each source with mode='fast' once" in server_source
    assert "one mode='full' upgrade per source" in server_source
    assert "do not split or restage the source" in server_source
    assert "exact structured video_id byte-for-byte" in server_source
    assert "instead of searching plugin caches" in server_source
    assert "view='compact'" in server_source
    assert "copy render_markdown byte-for-byte" in server_source
    assert "SHOW OR SHARE VIDEO IMAGES" in server_source
    assert "CODE OR TERMINAL CONTENT ONLY" in server_source
    assert "channel='all', never 'both'" in server_source
    assert "coherent nearby context" in server_source
    assert "progress update may state the requested retrieval goal" in server_source
    assert "never infer objects or layout from " in server_source
    assert '"OCR. Before retrieval' in server_source
    assert "SINGLE-IMAGE SAFETY" in server_source
    assert "SINGLE-IMAGE RESPONSE CONTRACT" in server_source
    assert "render_markdown byte-for-byte your entire" in server_source
    assert "TOPIC DISCOVERY CONTRACT" in server_source
    assert "does not make Keyframe the subject" in server_source
    assert "Keyframe does not search the public web" in server_source
    assert "central subject and instructional task strongly match" in server_source
    assert "With video_id omitted this" in server_source
    assert "existing local Keyframe library" in server_source


def test_mac_plugin_eval_covers_no_vision_and_forward_frame_rendering() -> None:
    suite = _load(ROOT / "evals" / "mac-plugin-cases.json")
    assert "MOTHERBOARD_VIDEO_ID" not in suite["variables"]
    cases = {case["id"]: case for case in suite["cases"]}
    no_vision = cases["desktop-share-frame-directly"]
    forward = cases["desktop-share-frame-forward-vision"]

    assert no_vision["model"] == "gpt-5.3-codex-spark"
    no_vision_criteria = " ".join(no_vision["success_criteria"])
    for requirement in (
        "under 30 seconds",
        "exactly once",
        "render_markdown byte-for-byte",
        "angle-bracket destination delimiters",
        "sole MCP image block bytes",
        "no browser, shell/terminal command, web-download",
        "Does not load SKILL.md",
        "Never requests quality=source",
        "entire final response",
        "no metadata, OCR",
        "unsupported visual claim",
        "exactly one cache-hit video_ingest",
        "between 4725 and 4741 seconds",
    ):
        assert requirement in no_vision_criteria

    skill_ui_paths = [
        PLUGIN / "skills" / "keyframe-video-rag" / "agents" / "openai.yaml",
        ROOT / ".agents" / "skills" / "keyframe-video-rag" / "agents" / "openai.yaml",
        ROOT / ".claude" / "skills" / "keyframe-video-rag" / "agents" / "openai.yaml",
    ]
    skill_ui_contents = [path.read_text(encoding="utf-8") for path in skill_ui_paths]
    assert skill_ui_contents[0] == skill_ui_contents[1] == skill_ui_contents[2]
    assert "allow_implicit_invocation: false" in skill_ui_contents[0]
    assert "preserve my topic" in skill_ui_contents[0]
    assert "find a strongly relevant public video" in skill_ui_contents[0]

    assert forward["model"] == "image-capable GPT-5.6"
    forward_criteria = " ".join(forward["success_criteria"])
    assert "description is based on image inspection" in forward_criteria
    assert "matches the displayed frame" in forward_criteria
    assert "between 4719 and 4741 seconds" in forward_criteria
    assert "render_markdown byte-for-byte" in forward_criteria


def test_mac_plugin_eval_covers_topic_aware_discovery() -> None:
    suite = _load(ROOT / "evals" / "mac-plugin-cases.json")
    cases = {case["id"]: case for case in suite["cases"]}

    ordinary = cases["desktop-topic-discovery-ordinary-meaning"]
    assert ordinary["model"] == "Luna 5.6 medium"
    assert ordinary["prompt"] == (
        "[@keyframe] How can I build my own processor? Are there any videos about it?"
    )
    ordinary_criteria = " ".join(ordinary["success_criteria"])
    for requirement in (
        "interprets processor as a CPU",
        "host web search before Keyframe ingestion",
        "do not add Keyframe, keyframes, or video processor",
        "at most three individual public videos",
        "calls video_ingest exactly once",
        "status=ready plus a video_id",
        "bounded Keyframe evidence call",
        "Attributes URL discovery to host web search",
        "at most two additional relevant links",
    ):
        assert requirement in ordinary_criteria

    contextual_case = cases["desktop-topic-discovery-contextual-meaning"]
    assert contextual_case["model"] == "Luna 5.6 medium"
    assert contextual_case["prompt"] == (
        "[@keyframe] I mean a custom video-processing pipeline, not a CPU. "
        "Are there videos about building one?"
    )
    contextual = " ".join(contextual_case["success_criteria"])
    assert "video-processing-pipeline meaning" in contextual
    assert "rather than forcing the CPU interpretation" in contextual
    assert "direct individual-public-video watch URL" in contextual
    assert "same-source duration retry" in contextual
    assert "does not auto-ingest an adjacent fallback" in contextual

    weak_case = cases["desktop-topic-discovery-weak-match"]
    assert weak_case["model"] == "Luna 5.6 medium"
    assert "{{WEAK_DISCOVERY_TOPIC}}" in weak_case["prompt"]
    weak = " ".join(weak_case["success_criteria"])
    assert "not a strong match" in weak
    assert "Does not call video_ingest" in weak
    assert "asks the user to choose or refine" in weak

    no_web_case = cases["desktop-topic-discovery-no-web"]
    assert no_web_case["model"] == "Luna 5.6 medium with host web search disabled"
    assert no_web_case["prompt"] == ordinary["prompt"]
    no_web = " ".join(no_web_case["success_criteria"])
    assert "does not pretend the local Keyframe library is public-web discovery" in no_web
    assert "does not call video_search as an internet substitute" in no_web
    assert "needs a supplied or externally discovered URL" in no_web


def test_release_version_is_synchronized_across_runtime_lock_evals_and_docs() -> None:
    runtime_source = (ROOT / "src" / "video_context_mcp" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert f'__version__ = "{PACKAGE_VERSION}"' in runtime_source

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    root_package = next(
        package for package in lock["package"] if package["name"] == "video-context-mcp"
    )
    assert root_package["version"] == PACKAGE_VERSION

    assert _load(ROOT / "evals" / "cases.json")["suite"] == (
        f"keyframe-v{PACKAGE_VERSION}"
    )
    assert _load(ROOT / "evals" / "mac-plugin-cases.json")["suite"] == (
        f"keyframe-mac-plugin-v{PACKAGE_VERSION}"
    )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    client_setup = (ROOT / "docs" / "client-setup.md").read_text(encoding="utf-8")
    for content in (readme, client_setup):
        assert f"video-context-mcp[whisper]=={PACKAGE_VERSION}" in content
        assert f"--ref v{PACKAGE_VERSION}" in content

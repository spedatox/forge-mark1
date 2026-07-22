"""Skills and MCP over the seams (H10).

Nothing here touches a core module. That is the claim MARK2_SEAMS made about
what an extension would cost once the seams existed, and these tests are what
cashing it looks like.
"""
import asyncio
import json
import sys
import textwrap

import pytest

from forge.agents.config import AgentConfig, CellSpec
from forge.extensions import load_extensions
from forge.mcp.client import MCPClient, MCPServerSpec
from forge.mcp.provider import MCPToolProvider
from forge.skills.loader import load_skills, parse_frontmatter
from forge.skills.provider import SkillProvider, SkillTool
from forge.warden.toolsource import fold_providers


def _cfg(tools=("read_file",)) -> AgentConfig:
    return AgentConfig(agent_id="t", name="T", domain="d", model_ref="scripted",
                       tool_names=tuple(tools), system_prompt="You are T.",
                       cell=CellSpec())


def write_skill(root, name, description="does a thing", body="Step one.\nStep two.",
                extra=""):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n",
        encoding="utf-8")
    return folder


# ── Frontmatter ──────────────────────────────────────────────────────────────
def test_frontmatter_is_split_from_the_body():
    meta, body = parse_frontmatter("---\nname: deploy\ndescription: ships it\n---\n\nDo this.")
    assert meta == {"name": "deploy", "description": "ships it"}
    assert body.strip() == "Do this."


def test_a_file_without_frontmatter_is_all_body():
    meta, body = parse_frontmatter("Just instructions.")
    assert meta == {} and body == "Just instructions."


# ── Loading ──────────────────────────────────────────────────────────────────
def test_skills_load_from_a_directory(tmp_path):
    write_skill(tmp_path, "deploy", "how to ship to production")
    write_skill(tmp_path, "rollback", "how to undo a bad ship")
    skills = load_skills(tmp_path)
    assert set(skills) == {"deploy", "rollback"}
    assert skills["deploy"].description == "how to ship to production"


def test_a_malformed_skill_is_skipped_not_fatal(tmp_path):
    """One file with a typo must not hold the operator's other skills hostage."""
    write_skill(tmp_path, "good")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("no frontmatter and no description", encoding="utf-8")
    assert set(load_skills(tmp_path)) == {"good"}


def test_an_earlier_root_wins(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_skill(a, "deploy", "the authoritative one")
    write_skill(b, "deploy", "the shadowed one")
    assert load_skills(a, b)["deploy"].description == "the authoritative one"


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert load_skills(tmp_path / "nope") == {}


# ── The tool ─────────────────────────────────────────────────────────────────
def test_the_catalogue_reaches_the_prompt_but_the_body_does_not(tmp_path):
    """Injecting twenty skill bodies into every job would spend the window on
    procedures the job will never touch."""
    write_skill(tmp_path, "deploy", "how to ship", body="SECRET STEP DETAIL")
    provider = SkillProvider.from_dirs(tmp_path)
    fragment = provider.fragment()

    assert "deploy: how to ship" in fragment.text
    assert "SECRET STEP DETAIL" not in fragment.text


def test_loading_a_skill_returns_its_instructions(tmp_path):
    write_skill(tmp_path, "deploy", body="Run the migration first.")
    tool = SkillTool(load_skills(tmp_path))
    out = asyncio.run(tool.call(tool.Args(name="deploy"), None))
    assert "Run the migration first." in out.content and not out.is_error


def test_an_unknown_skill_lists_what_exists(tmp_path):
    write_skill(tmp_path, "deploy")
    tool = SkillTool(load_skills(tmp_path))
    out = asyncio.run(tool.call(tool.Args(name="nope"), None))
    assert out.is_error and "deploy" in out.content


def test_allowed_tools_is_guidance_and_never_a_grant(tmp_path):
    """A file dropped in a directory must not be able to give the agent
    capabilities the operator's profile withheld."""
    write_skill(tmp_path, "deploy", extra="allowed-tools: run_command, write_file\n")
    provider = SkillProvider.from_dirs(tmp_path)
    tools = asyncio.run(fold_providers([provider], _cfg(), None))

    assert set(tools) == {"skill"}, "the skill tool only — not the tools it names"
    out = asyncio.run(tools["skill"].call(tools["skill"].Args(name="deploy"), None))
    assert "run_command" in out.content        # surfaced as guidance


def test_no_skills_means_no_tool(tmp_path):
    """A tool whose every call can only fail is worse than no tool."""
    provider = SkillProvider.from_dirs(tmp_path)
    assert asyncio.run(fold_providers([provider], _cfg(), None)) == {}
    assert provider.fragment() is None


# ── MCP ──────────────────────────────────────────────────────────────────────
FAKE_SERVER = textwrap.dedent('''
    import json, sys
    TOOLS = [
        {"name": "tide", "description": "reports the tide",
         "inputSchema": {"type": "object", "properties": {"port": {"type": "string"}},
                         "required": ["port"]},
         "annotations": {"readOnlyHint": True, "idempotentHint": True}},
        {"name": "dredge", "description": "moves a lot of mud",
         "inputSchema": {"type": "object", "properties": {}}},
    ]
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            continue
        if method == "initialize":
            out = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}}
        elif method == "tools/list":
            out = {"tools": TOOLS}
        elif method == "tools/call":
            args = msg["params"].get("arguments", {})
            out = {"content": [{"type": "text",
                                "text": f"tide at {args.get('port')} is high"}]}
        else:
            out = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": out}) + "\\n")
        sys.stdout.flush()
''')


@pytest.fixture
def fake_server(tmp_path):
    path = tmp_path / "server.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return MCPServerSpec(name="harbour", command=sys.executable, args=(str(path),))


def test_an_mcp_server_contributes_prefixed_tools(fake_server):
    provider = MCPToolProvider(fake_server)

    async def scenario():
        tools = await provider.provide(_cfg(), None)
        await provider.close()
        return tools

    tools = asyncio.run(scenario())
    # Prefixed, or two servers offering `search` would collide at the fold and
    # adding a second server would break the first.
    assert set(tools) == {"mcp__harbour__tide", "mcp__harbour__dredge"}


def test_remote_flags_fail_closed(fake_server):
    provider = MCPToolProvider(fake_server)

    async def scenario():
        tools = await provider.provide(_cfg(), None)
        await provider.close()
        return tools

    tools = asyncio.run(scenario())
    tide, dredge = tools["mcp__harbour__tide"], tools["mcp__harbour__dredge"]
    tide_args = tide.Args(port="Rotterdam")
    assert tide.is_read_only(tide_args) is True
    assert tide.is_concurrency_safe(tide_args) is True
    # dredge declares nothing, so it gets nothing: silence is not a safety claim.
    assert dredge.is_read_only(dredge.Args()) is False
    assert dredge.is_concurrency_safe(dredge.Args()) is False


def test_a_remote_tool_call_proxies_and_returns_text(fake_server):
    provider = MCPToolProvider(fake_server)

    async def scenario():
        tools = await provider.provide(_cfg(), None)
        tool = tools["mcp__harbour__tide"]
        result = await tool.call(tool.Args(port="Rotterdam"), None)
        await provider.close()
        return result

    result = asyncio.run(scenario())
    assert result.content == "tide at Rotterdam is high" and not result.is_error


def test_a_server_that_will_not_start_contributes_nothing(tmp_path):
    """A broken integration must not stop a job that never needed it."""
    provider = MCPToolProvider(MCPServerSpec(name="ghost", command="definitely-not-a-real-binary"))

    async def scenario():
        tools = await provider.provide(_cfg(), None)
        await provider.close()
        return tools

    assert asyncio.run(scenario()) == {}


def test_the_server_schema_is_what_the_model_sees(fake_server):
    """Our pydantic model is a lenient approximation; the server's own schema
    carries the descriptions and constraints the model should read."""
    provider = MCPToolProvider(fake_server)

    async def scenario():
        tools = await provider.provide(_cfg(), None)
        await provider.close()
        return tools["mcp__harbour__tide"].schema()

    schema = asyncio.run(scenario())
    assert schema["input_schema"]["required"] == ["port"]


# ── Assembly ─────────────────────────────────────────────────────────────────
def test_no_config_means_builtins_only(tmp_path):
    ext = load_extensions(tmp_path / "absent.json", tmp_path / "absent-skills")
    assert [p.name for p in ext.tool_providers()] == ["builtin"]
    assert ext.fragments == []


def test_config_assembles_skills_and_servers(tmp_path):
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir, "deploy")
    (tmp_path / "extensions.json").write_text(json.dumps({
        "skillsDirs": [str(skills_dir)],
        "mcpServers": {"harbour": {"command": "echo", "args": ["hi"]}},
    }), encoding="utf-8")

    ext = load_extensions(tmp_path / "extensions.json", tmp_path / "unused")
    assert [p.name for p in ext.tool_providers()] == ["builtin", "skills", "mcp:harbour"]
    assert len(ext.fragments) == 1


def test_builtins_come_first_so_an_extension_cannot_shadow_one(tmp_path):
    """The fold refuses collisions, and refusing means the later source loses."""
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir, "deploy")
    ext = load_extensions(tmp_path / "absent.json", skills_dir)
    assert ext.tool_providers()[0].name == "builtin"


def test_a_broken_config_is_not_fatal(tmp_path):
    (tmp_path / "extensions.json").write_text("{ this is not json", encoding="utf-8")
    ext = load_extensions(tmp_path / "extensions.json", tmp_path / "absent")
    assert [p.name for p in ext.tool_providers()] == ["builtin"]


def test_a_server_entry_without_a_command_is_skipped(tmp_path):
    (tmp_path / "extensions.json").write_text(
        json.dumps({"mcpServers": {"bad": {"args": ["x"]}}}), encoding="utf-8")
    ext = load_extensions(tmp_path / "extensions.json", tmp_path / "absent")
    assert [p.name for p in ext.tool_providers()] == ["builtin"]

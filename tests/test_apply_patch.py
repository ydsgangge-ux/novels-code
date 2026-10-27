"""apply_patch: parsing, fuzzy anchoring, multi-file application, containment."""

from __future__ import annotations

import pytest

from gangge.layer3_agent.tools.patch import (
    PatchError,
    apply_file_ops,
    parse_patch,
    seek_sequence,
)

SAMPLE = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "mathutils.py").write_text(SAMPLE, encoding="utf-8")
    return tmp_path


# ── Parsing ────────────────────────────────────────────────────


def test_parse_update_chunk():
    ops = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: mathutils.py\n"
        "@@ def sub\n"
        " def sub(a, b):\n"
        "-    return a - b\n"
        "+    return a - b  # fixed\n"
        "*** End Patch"
    )
    assert len(ops) == 1
    assert ops[0].kind == "update"
    assert ops[0].chunks[0].old == ["def sub(a, b):", "    return a - b"]
    assert ops[0].chunks[0].new == ["def sub(a, b):", "    return a - b  # fixed"]


def test_parse_rejects_missing_markers():
    with pytest.raises(PatchError, match="Begin Patch"):
        parse_patch("*** Update File: a.py\n+ x")
    with pytest.raises(PatchError, match="End Patch"):
        parse_patch("*** Begin Patch\n*** Update File: a.py\n+ x")


def test_parse_add_file_requires_plus_prefix():
    with pytest.raises(PatchError, match="必须以 '\\+' 开头"):
        parse_patch("*** Begin Patch\n*** Add File: x.py\nprint(1)\n*** End Patch")


def test_parse_strips_code_fence():
    ops = parse_patch(
        "```\n*** Begin Patch\n*** Add File: x.py\n+print(1)\n*** End Patch\n```"
    )
    assert ops[0].add_lines == ["print(1)"]


# ── Anchoring ──────────────────────────────────────────────────


def test_seek_exact_then_trimmed_then_fuzzy():
    file_lines = ["def a():", "    return 1", "def b():", "    return 2"]

    assert seek_sequence(file_lines, ["def b():", "    return 2"]) == (2, "exact")
    # Same text, different indentation → trimmed match
    assert seek_sequence(file_lines, ["def b():", "  return 2"]) == (2, "trimmed")
    # One character off → fuzzy match still lands on the right block
    index, strategy = seek_sequence(file_lines, ["def b():", "    return 3"])
    assert index == 2 and strategy == "fuzzy"


def test_seek_missing_returns_minus_one():
    assert seek_sequence(["a", "b"], ["zzz", "yyy", "xxx"])[0] == -1


# ── Application ────────────────────────────────────────────────


def test_apply_update(workspace):
    ops = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: mathutils.py\n"
        " def sub(a, b):\n"
        "-    return a - b\n"
        "+    return a - b  # fixed\n"
        "*** End Patch"
    )
    records = apply_file_ops(ops, workspace)
    content = (workspace / "mathutils.py").read_text(encoding="utf-8")
    assert "return a - b  # fixed" in content
    assert content.endswith("\n")
    assert records[0]["added"] == 2 and records[0]["removed"] == 2
    assert records[0]["diff"]


def test_apply_add_and_delete(workspace):
    ops = parse_patch(
        "*** Begin Patch\n"
        "*** Add File: extra.py\n"
        "+x = 1\n"
        "+y = 2\n"
        "*** Delete File: mathutils.py\n"
        "*** End Patch"
    )
    apply_file_ops(ops, workspace)
    assert (workspace / "extra.py").read_text(encoding="utf-8") == "x = 1\ny = 2\n"
    assert not (workspace / "mathutils.py").exists()


def test_apply_move(workspace):
    ops = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: mathutils.py\n"
        "*** Move to: renamed.py\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a + b  # sum\n"
        "*** End Patch"
    )
    apply_file_ops(ops, workspace)
    assert not (workspace / "mathutils.py").exists()
    assert "sum" in (workspace / "renamed.py").read_text(encoding="utf-8")


def test_apply_add_existing_file_is_refused(workspace):
    ops = parse_patch("*** Begin Patch\n*** Add File: mathutils.py\n+x\n*** End Patch")
    with pytest.raises(PatchError, match="已存在"):
        apply_file_ops(ops, workspace)


def test_apply_update_missing_file_points_to_add(workspace):
    ops = parse_patch(
        "*** Begin Patch\n*** Update File: nope.py\n a\n-b\n+c\n*** End Patch"
    )
    with pytest.raises(PatchError, match="Add File"):
        apply_file_ops(ops, workspace)


def test_apply_unmatched_context_explains(workspace):
    ops = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: mathutils.py\n"
        " totally different line\n"
        "- nothing like this\n"
        "+ something\n"
        "*** End Patch"
    )
    with pytest.raises(PatchError, match="无法定位"):
        apply_file_ops(ops, workspace)


def test_path_escaping_workspace_is_refused(workspace):
    ops = parse_patch("*** Begin Patch\n*** Add File: ../outside.py\n+x\n*** End Patch")
    with pytest.raises(PatchError, match="工作区之外"):
        apply_file_ops(ops, workspace)
    assert not (workspace.parent / "outside.py").exists()


def test_multiple_chunks_in_one_file(workspace):
    ops = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: mathutils.py\n"
        "@@ def add\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a + b  # sum\n"
        "@@ def sub\n"
        " def sub(a, b):\n"
        "-    return a - b\n"
        "+    return a - b  # diff\n"
        "*** End Patch"
    )
    apply_file_ops(ops, workspace)
    content = (workspace / "mathutils.py").read_text(encoding="utf-8")
    assert "# sum" in content and "# diff" in content


@pytest.mark.asyncio
async def test_tool_returns_diff_metadata(workspace):
    from gangge.layer3_agent.tools.patch import ApplyPatchTool

    tool = ApplyPatchTool(workspace=str(workspace))
    result = await tool.execute(
        patch=(
            "*** Begin Patch\n"
            "*** Update File: mathutils.py\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    return a + b  # sum\n"
            "*** End Patch"
        )
    )
    assert not result.is_error
    assert "+    return a + b  # sum" in result.metadata["diff"]
    assert result.metadata["lines_added"] == 2


@pytest.mark.asyncio
async def test_tool_reports_bad_patch_without_raising(workspace):
    from gangge.layer3_agent.tools.patch import ApplyPatchTool

    tool = ApplyPatchTool(workspace=str(workspace))
    result = await tool.execute(patch="not a patch at all")
    assert result.is_error
    assert "补丁解析失败" in result.output

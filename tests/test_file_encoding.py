"""编辑工具必须保留原文件的编码、BOM 和换行风格。

回归的是两个静默损坏：
1. GBK 文件被 read_text(errors="replace") 读成 U+FFFD，再以 UTF-8 写回 ——
   中文全坏，编码也变了，全程不报错。
2. read_text 把换行统一成 LF，write_text 再按 os.linesep 写回 —— LF 文件在
   Windows 上整份变成 CRLF，git 显示全量改动。
"""
from __future__ import annotations

import pytest

from gangge.layer3_agent.tools.file_ops import EditFileTool
from gangge.layer3_agent.tools.patch import ApplyPatchTool


@pytest.mark.asyncio
async def test_gbk_file_keeps_its_encoding(tmp_path):
    target = tmp_path / "gbk.py"
    original = "# 中文注释\nvalue = 1\n"
    target.write_bytes(original.encode("gbk"))

    tool = EditFileTool(workspace=str(tmp_path))
    result = await tool.execute(path="gbk.py", old_str="value = 1", new_str="value = 2")

    assert not result.is_error, result.output
    assert target.read_bytes() == "# 中文注释\nvalue = 2\n".encode("gbk")
    # 不是 UTF-8
    with pytest.raises(UnicodeDecodeError):
        target.read_bytes().decode("utf-8")


@pytest.mark.asyncio
async def test_crlf_file_stays_crlf(tmp_path):
    target = tmp_path / "crlf.py"
    target.write_bytes(b"a = 1\r\nb = 2\r\n")

    tool = EditFileTool(workspace=str(tmp_path))
    assert not (await tool.execute(path="crlf.py", old_str="a = 1", new_str="a = 3")).is_error

    assert target.read_bytes() == b"a = 3\r\nb = 2\r\n"


@pytest.mark.asyncio
async def test_lf_file_stays_lf(tmp_path):
    target = tmp_path / "lf.py"
    target.write_bytes(b"a = 1\nb = 2\n")

    tool = EditFileTool(workspace=str(tmp_path))
    assert not (await tool.execute(path="lf.py", old_str="a = 1", new_str="a = 3")).is_error

    assert target.read_bytes() == b"a = 3\nb = 2\n"


@pytest.mark.asyncio
async def test_utf8_bom_is_preserved(tmp_path):
    target = tmp_path / "bom.py"
    target.write_bytes(b"\xef\xbb\xbfx = 1\n")

    tool = EditFileTool(workspace=str(tmp_path))
    assert not (await tool.execute(path="bom.py", old_str="x = 1", new_str="x = 2")).is_error

    assert target.read_bytes() == b"\xef\xbb\xbfx = 2\n"


@pytest.mark.asyncio
async def test_binary_file_is_refused(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\x01\x02abc")

    tool = EditFileTool(workspace=str(tmp_path))
    result = await tool.execute(path="blob.bin", old_str="abc", new_str="def")

    assert result.is_error
    assert target.read_bytes() == b"\x00\x01\x02abc"


@pytest.mark.asyncio
async def test_apply_patch_preserves_crlf(tmp_path):
    target = tmp_path / "p.py"
    target.write_bytes(b"x = 1\r\ny = 2\r\n")

    patch = (
        "*** Begin Patch\n"
        "*** Update File: p.py\n"
        "@@\n"
        " x = 1\n"
        "-y = 2\n"
        "+y = 9\n"
        "*** End Patch"
    )
    tool = ApplyPatchTool(workspace=str(tmp_path))
    result = await tool.execute(patch=patch)

    assert not result.is_error, result.output
    assert target.read_bytes() == b"x = 1\r\ny = 9\r\n"

"""Web 后端的会话管理契约。

这里只测"不需要真实模型"的部分：建/删会话、参数校验、未等待时的回复、
静态页面。真正的任务执行要打 LLM，留给手动冒烟（``python -m gangge.web.server``）。

多标签能同时跑，靠的是每会话一个线程 + 一个独立 cancel 标志，所以这里也确认
"会话之间不共享状态"这条：两个会话各自的 workspace 不同，清单互不可见。
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from gangge.web import server as web_server  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    """每个用例一个独立会话库，绝不碰仓库里真实的 .gangge_data。"""
    from gangge.runtime.session_db import SessionDB

    web_server._SESSIONS.clear()
    store = SessionDB(str(tmp_path / "sessions.db"))
    store.connect()
    web_server._DB = store
    try:
        with TestClient(web_server.app) as c:
            yield c
    finally:
        web_server._SESSIONS.clear()
        store.close()
        web_server._DB = None


def _new(client, tmp_path, name: str = "t") -> str:
    res = client.post(
        "/api/sessions",
        json={"workspace": str(tmp_path), "name": name},
    )
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_index_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Gangge Web" in res.text


def test_create_list_and_delete(client, tmp_path):
    sid = _new(client, tmp_path, "写文档")

    listed = client.get("/api/sessions").json()["sessions"]
    assert [s["id"] for s in listed] == [sid]
    assert listed[0]["workspace"] == str(tmp_path.resolve())
    assert listed[0]["status"] == "idle"

    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.get("/api/sessions").json()["sessions"] == []


def test_unknown_session_is_404(client):
    assert client.get("/api/sessions/nope/todos").status_code == 404
    assert client.post("/api/sessions/nope/cancel").status_code == 404


def test_blank_message_is_rejected(client, tmp_path):
    sid = _new(client, tmp_path)
    res = client.post(f"/api/sessions/{sid}/messages", json={"text": "   "})
    assert res.status_code == 400


def test_reply_without_a_pending_question_is_rejected(client, tmp_path):
    sid = _new(client, tmp_path)
    res = client.post(f"/api/sessions/{sid}/reply", json={"value": "x"})
    assert res.status_code == 409


def test_sessions_do_not_share_a_todo_list(client, tmp_path):
    from gangge.layer3_agent.tools import todo as todo_mod

    todo_mod._states_by_workspace.clear()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    sid_a = _new(client, a, "A")
    sid_b = _new(client, b, "B")

    from gangge.layer3_agent.tools.todo import get_state_for

    get_state_for(str(a)).update([{"id": "1", "content": "只属于 A", "status": "pending"}])

    todos_a = client.get(f"/api/sessions/{sid_a}/todos").json()["todos"]
    todos_b = client.get(f"/api/sessions/{sid_b}/todos").json()["todos"]

    assert [t["content"] for t in todos_a] == ["只属于 A"]
    assert todos_b == [], "另一个会话不该看到别人的清单"
    todo_mod._states_by_workspace.clear()


def test_max_rounds_is_clamped(client, tmp_path):
    res = client.post(
        "/api/sessions",
        json={"workspace": str(tmp_path), "max_rounds": 100000},
    )
    assert res.json()["max_rounds"] == 200, "轮数上限要夹住，防止一个标签把机器跑满"


# ── 文件浏览 ──
def test_file_browser_lists_and_reads(client, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    sid = _new(client, tmp_path)

    root = client.get(f"/api/sessions/{sid}/files").json()
    assert "src" in {e["name"] for e in root["entries"]}

    sub = client.get(f"/api/sessions/{sid}/files", params={"path": "src"}).json()
    assert [e["name"] for e in sub["entries"]] == ["main.py"]

    body = client.get(f"/api/sessions/{sid}/file", params={"path": "src/main.py"}).json()
    assert body["content"].strip() == "print(1)", "Windows 换行不该影响内容判断"


def test_file_browser_skips_vendor_dirs(client, tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("1", encoding="utf-8")
    (tmp_path / "app.py").write_text("x=1", encoding="utf-8")
    sid = _new(client, tmp_path)

    names = {e["name"] for e in client.get(f"/api/sessions/{sid}/files").json()["entries"]}
    assert "node_modules" not in names
    assert "app.py" in names


def test_file_browser_rejects_traversal(client, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    sid = _new(client, ws)

    res = client.get(f"/api/sessions/{sid}/file", params={"path": "../secret.txt"})
    assert res.status_code == 403, "工作目录之外的文件不给读"


# ── 重命名 / 历史 / 排队 ──
def test_rename_updates_listing(client, tmp_path):
    sid = _new(client, tmp_path, "旧名字")
    assert client.post(f"/api/sessions/{sid}/rename", json={"name": "新名字"}).json()["name"] == "新名字"
    assert client.get("/api/sessions").json()["sessions"][0]["name"] == "新名字"


def test_history_reads_from_disk_and_reopens(client, tmp_path):
    sid = _new(client, tmp_path, "会持久化")
    rows = client.get("/api/history").json()["sessions"]
    assert any(r["id"] == sid and r["title"] == "会持久化" for r in rows)

    web_server._SESSIONS.clear()   # 模拟服务重启
    snap = client.post("/api/sessions/open", json={"id": sid}).json()
    assert snap["id"] == sid
    assert snap["workspace"] == str(tmp_path.resolve())


def test_open_unknown_history_id_is_404(client):
    assert client.post("/api/sessions/open", json={"id": "nope"}).status_code == 404


def test_message_queues_while_busy(client, tmp_path):
    import threading

    sid = _new(client, tmp_path)
    session = web_server._SESSIONS[sid]
    gate = threading.Event()
    worker = threading.Thread(target=gate.wait, daemon=True)
    worker.start()
    session.thread = worker
    try:
        res = client.post(f"/api/sessions/{sid}/messages", json={"text": "再补一条"})
        assert res.status_code == 200
        assert res.json().get("queued") is True
        # 队列项从字符串改成了 dict：text 进气泡、task 喂模型（可能带附件说明）
        assert len(session.queue_messages) == 1
        queued = session.queue_messages[0]
        assert queued["text"] == "再补一条"
        assert queued["task"] == "再补一条"      # 没有附件时两者相同
    finally:
        gate.set()
        worker.join(timeout=2)


def test_models_endpoint_has_shape(client):
    data = client.get("/api/models").json()
    assert "current" in data and "options" in data

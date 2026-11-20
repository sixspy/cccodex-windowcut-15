"""仪表同源与两人关窗争用：先红后绿。

口径（见 ops/仪表同源册.md，2026-11-20 定版）：
- 乙路径：dash_counter 影子表 + 关窗/开窗同事务刷新；仪表卡与窗口条带的
  窗口计数只读该影子表，禁止再聚合 windows 实表，也禁止缓存脏读。
- 两处计数必须同源；后到关窗只记争用，不写业务、不刷计数。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import db
from app import app


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "dash.db")
    db.init_db()
    yield


def _auth(c: TestClient, username="admin", password="123456") -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _dash(c, h):
    return c.get("/api/dashboard", headers=h).json()


def _strip(c, h):
    body = c.get("/api/windows", headers=h).json()
    return body, body.get("counts", {})


def _counter_rows():
    with db.connect() as conn:
        return {
            r["key"]: r["value"]
            for r in conn.execute("SELECT key,value FROM dash_counter").fetchall()
        }


def test_counting_split_fixture_repairs_to_same_source():
    """夹具：重开空桩幽灵窗 + 漂移的影子计数，仪表与条带必须同源收敛。"""
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO reopen_ledger(window_id,from_generation,to_generation,mode,status,created_at)
               VALUES (1,1,2,'作废重算','空桩登记','2026-11-02T09:00:00')"""
        )
        try:
            conn.execute(
                """INSERT INTO dash_counter(key,value,updated_at) VALUES
                   ('open_windows',1,'2026-11-02T09:00:00'),
                   ('closed_windows',3,'2026-11-02T09:00:00')
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value"""
            )
        except sqlite3.OperationalError:
            # 未落影子表的旧实现：测试应当直接见红
            pass
        conn.commit()

    # 模拟进程重启：启动侧必须把幽灵台账收敛进 windows 并刷新影子计数
    db.init_db()

    c = TestClient(app)
    h = _auth(c)
    dash = _dash(c, h)
    _, counts = _strip(c, h)
    counters = _counter_rows()

    # 五处同态之一：windows 实表被补执行重开
    with db.connect() as conn:
        w1 = conn.execute("SELECT * FROM windows WHERE id=1").fetchone()
        led = conn.execute(
            "SELECT status,to_generation FROM reopen_ledger WHERE window_id=1"
        ).fetchone()
    assert w1["status"] == "开启" and w1["generation"] == 2, "重开空桩幽灵窗未收敛"
    assert led["status"] != "空桩登记"

    # 仪表 vs 条带 vs 影子表：同一份数字
    assert dash["open_windows"] == counts["open_windows"] == counters["open_windows"] == 3
    assert dash["closed_windows"] == counts["closed_windows"] == counters["closed_windows"] == 0


def test_later_closer_only_records_contention():
    """甲关窗仍占窗锁时，乙关窗只能记争用；业务与计数都不许动。"""
    c = TestClient(app)
    h1 = _auth(c)
    h2 = _auth(c, "closer", "close123")

    # 第一个关窗者已占窗锁（等价于它正在关窗、计数刷新尚未提交）
    assert c.post(
        "/api/locks/acquire", headers=h1, json={"lock_key": "window:2"}
    ).status_code == 200

    r = c.post("/api/windows/2/close", headers=h2)
    assert r.status_code == 409, "后到者未被窗锁拒绝"
    assert "争用" in r.json().get("detail", "")

    cont = c.get("/api/contention", headers=h1).json()["items"]
    raced = [x for x in cont if x["lock_key"] == "window:2" and x["actor"] == "closer"]
    assert len(raced) == 1, "争用登记被事务回滚吞掉"

    with db.connect() as conn:
        w = conn.execute("SELECT status FROM windows WHERE id=2").fetchone()
        close_audit = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='window_close' AND target='2'"
        ).fetchone()["c"]
    assert w["status"] == "开启", "后到者写了业务"
    assert close_audit == 0, "后到者写了关窗审计"

    dash = _dash(c, h1)
    _, counts = _strip(c, h1)
    counters = _counter_rows()
    assert dash["open_windows"] == counts["open_windows"] == counters["open_windows"] == 2
    assert dash["closed_windows"] == counts["closed_windows"] == counters["closed_windows"] == 1
    assert dash["contention"] == counters["contention"] == 1

    # 持锁者继续完成关窗，五处同态
    assert c.post("/api/windows/2/close", headers=h1).status_code == 200
    dash = _dash(c, h1)
    _, counts = _strip(c, h1)
    assert dash["open_windows"] == counts["open_windows"] == 1
    assert dash["closed_windows"] == counts["closed_windows"] == 2


def test_counter_refresh_failure_rolls_back_business(monkeypatch):
    """关窗与计数刷新同事务：刷新失败则窗口状态与影子计数一起回滚。"""
    import windows_svc

    def _boom(conn, *args, **kwargs):
        raise RuntimeError("dash_counter 刷新失败（夹具）")

    monkeypatch.setattr(windows_svc, "refresh_dash_counters", _boom)

    c = TestClient(app, raise_server_exceptions=False)
    h = _auth(c)
    r = c.post("/api/windows/2/close", headers=h)
    assert r.status_code == 500

    with db.connect() as conn:
        w = conn.execute("SELECT status,seal_token FROM windows WHERE id=2").fetchone()
        audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='window_close'"
        ).fetchone()["c"]
        locks = conn.execute("SELECT COUNT(*) c FROM window_locks").fetchone()["c"]
    assert w["status"] == "开启" and w["seal_token"] is None, "业务未随计数回滚"
    assert audits == 0
    assert locks == 0, "失败后窗锁未释放"

    dash = _dash(c, h)
    _, counts = _strip(c, h)
    counters = _counter_rows()
    assert dash["open_windows"] == counts["open_windows"] == counters["open_windows"] == 2
    assert dash["closed_windows"] == counts["closed_windows"] == counters["closed_windows"] == 1


def test_deviation_rejected_and_logged(monkeypatch):
    """偏离同源册（计数刷新失败）拒写业务，并记「仪表偏了」。"""
    import reopen_svc

    def _boom(conn, *args, **kwargs):
        raise RuntimeError("dash_counter 刷新失败（夹具）")

    c = TestClient(app, raise_server_exceptions=False)
    h = _auth(c)
    assert c.post("/api/windows/1/close", headers=h).status_code == 200

    monkeypatch.setattr(reopen_svc, "refresh_dash_counters", _boom)
    r = c.post("/api/windows/1/reopen-stub", headers=h, json={"mode": "作废重算"})
    assert r.status_code == 500

    with db.connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE id=1").fetchone()
        acts = [x["action"] for x in conn.execute("SELECT action FROM audit_trail").fetchall()]
        locks = conn.execute("SELECT COUNT(*) c FROM window_locks").fetchone()["c"]
    assert w["status"] == "已关闭" and w["generation"] == 1, "偏离未拒写"
    assert "仪表偏了" in acts
    assert locks == 0, "偏离路径未释放窗锁"

    dash = _dash(c, h)
    _, counts = _strip(c, h)
    counters = _counter_rows()
    assert dash["open_windows"] == counts["open_windows"] == counters["open_windows"] == 2
    assert dash["closed_windows"] == counts["closed_windows"] == counters["closed_windows"] == 1


def test_business_rejection_is_not_dash_deviation():
    """404/409/400 是业务拒绝，不得记成「仪表偏了」。"""
    c = TestClient(app)
    h = _auth(c)
    assert c.post("/api/windows/999/close", headers=h).status_code == 404
    assert (
        c.post(
            "/api/windows/2/reopen-stub", headers=h, json={"mode": "作废重算"}
        ).status_code
        == 409
    )
    assert c.post("/api/windows/1/close", headers=h).status_code == 200
    assert (
        c.post(
            "/api/windows/1/reopen-stub", headers=h, json={"mode": "增量补丁"}
        ).status_code
        == 400
    )
    with db.connect() as conn:
        acts = [
            x["action"]
            for x in conn.execute("SELECT action FROM audit_trail").fetchall()
        ]
        locks = conn.execute("SELECT COUNT(*) c FROM window_locks").fetchone()["c"]
    assert "仪表偏了" not in acts
    assert locks == 0


def test_unselected_reopen_mode_rejected():
    """定版只落作废重算：另一口径拒写，不改五处任何数字。"""
    c = TestClient(app)
    h = _auth(c)
    assert c.post("/api/windows/1/close", headers=h).status_code == 200
    before = _dash(c, h)
    r = c.post("/api/windows/1/reopen-stub", headers=h, json={"mode": "增量补丁"})
    assert r.status_code == 400 and "同源册" in r.json()["detail"]
    after = _dash(c, h)
    with db.connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE id=1").fetchone()
        led = conn.execute("SELECT COUNT(*) c FROM reopen_ledger").fetchone()["c"]
    assert w["status"] == "已关闭" and w["generation"] == 1
    assert led == 0
    assert before == after


def test_reopen_moves_five_surfaces_together():
    """关闭→重开后，仪表卡/条带/台账/审计/计数影子同态。"""
    c = TestClient(app)
    h = _auth(c)
    assert c.post("/api/windows/2/close", headers=h).status_code == 200
    r = c.post(
        "/api/windows/2/reopen-stub", headers=h, json={"mode": "作废重算"}
    )
    assert r.status_code == 200
    assert r.json()["from_generation"] == 1
    assert r.json()["to_generation"] == 2

    with db.connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE id=2").fetchone()
        led = conn.execute(
            "SELECT * FROM reopen_ledger WHERE window_id=2 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        audit = conn.execute(
            "SELECT action FROM audit_trail WHERE target='2' ORDER BY id"
        ).fetchall()
    assert w["status"] == "开启" and w["generation"] == 2
    assert led["status"] == "已重开" and led["to_generation"] == 2
    actions = {a["action"] for a in audit}
    assert {"window_close", "window_reopen"} <= actions

    dash = _dash(c, h)
    _, counts = _strip(c, h)
    counters = _counter_rows()
    assert dash["open_windows"] == counts["open_windows"] == counters["open_windows"] == 2
    assert dash["closed_windows"] == counts["closed_windows"] == counters["closed_windows"] == 1

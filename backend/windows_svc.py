"""业务窗开/关/列表。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import (
    commit_with_counters,
    connect,
    read_dash_counters,
    record_dash_deviation,
    refresh_dash_counters,
    write_audit,
)
from locks import release, require_lock
from models import WindowCreateIn

router = APIRouter(tags=["windows"])


@router.get("/api/windows")
def list_windows(authorization: AuthHeader = None):
    """条带行来自实表，窗口计数与仪表同读 dash_counter（乙路径）。"""
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM windows ORDER BY id").fetchall()
        counts = read_dash_counters(conn)
        return {
            "items": [dict(r) for r in rows],
            "counts": {
                "open_windows": counts["open_windows"],
                "closed_windows": counts["closed_windows"],
                "reopens": counts["reopens"],
                "source": "dash_counter",
            },
        }


@router.post("/api/windows")
def create_window(body: WindowCreateIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO windows(name,open_at,close_at,status,generation) VALUES (?,?,?,?,1)",
                (body.name, body.open_at, body.close_at, "开启"),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(409, f"窗口创建失败: {exc}") from exc
        wid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        write_audit(conn, user, "window_create", body.name, {"id": wid})
        commit_with_counters(conn)
        row = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
        return dict(row)


@router.post("/api/windows/{wid}/close")
def close_window(wid: int, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"window:{wid}"
    with connect() as conn:
        # 占窗锁必须独立提交：后到者要能看见持锁行，只记争用。
        require_lock(conn, lock_key, user)
        try:
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            if w["status"] == "已关闭":
                release(conn, lock_key, user)
                conn.commit()
                return {"ok": True, "already": True}
            now = datetime.now().isoformat(timespec="seconds")
            token = f"seal-{wid}-{w['generation']}-{now}"
            conn.execute(
                "UPDATE windows SET status='已关闭', seal_token=? WHERE id=?",
                (token, wid),
            )
            write_audit(conn, user, "window_close", str(wid), {"seal_token": token})
            # 乙路径：业务与计数同事务；刷新失败则整笔回滚。
            refresh_dash_counters(conn)
        except HTTPException:
            # 业务拒绝（404/409 等）不是计数偏离，原样抛出。
            conn.rollback()
            release(conn, lock_key, user)
            conn.commit()
            raise
        except Exception as exc:
            # 偏离同源册：拒写并记「仪表偏了」。
            conn.rollback()
            release(conn, lock_key, user)
            record_dash_deviation(
                conn,
                user,
                str(wid),
                {"path": "乙", "stage": "window_close", "error": str(exc)},
            )
            raise
        release(conn, lock_key, user)
        conn.commit()
        return {"ok": True, "seal_token": token}


@router.get("/api/windows/{wid}")
def get_window(wid: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
        if not w:
            raise HTTPException(404, "窗口不存在")
        ev = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM events WHERE window_id=? AND disposition='窗内'",
            (wid,),
        ).fetchone()
        return {
            "window": dict(w),
            "in_window_count": ev["c"],
            "in_window_amount": ev["s"],
        }

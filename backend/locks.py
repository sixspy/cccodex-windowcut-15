"""窗口锁与争用登记（公共并发面，题面争用句应对得上）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import commit_with_counters, connect, refresh_dash_counters, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def try_acquire(conn, lock_key: str, holder: str, commit_contention: bool = False) -> bool:
    now = datetime.now().isoformat(timespec="seconds")
    # 原子占锁：INSERT ... ON CONFLICT DO NOTHING，避免 SELECT→INSERT 竞态两人都成功。
    cur = conn.execute(
        """INSERT INTO window_locks(lock_key,holder,acquired_at) VALUES (?,?,?)
           ON CONFLICT(lock_key) DO NOTHING""",
        (lock_key, holder, now),
    )
    if cur.rowcount == 1:
        return True
    row = conn.execute(
        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is not None and row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder'] if row else '?'}", now),
    )
    # 乙路径：争用计数与登记同事务；数据面调用方要求立即提交，避免被 409 回滚吞掉。
    refresh_dash_counters(conn)
    if commit_contention:
        conn.commit()
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM window_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    """占锁行独立提交，使并发后到者能看见持锁并只记争用。

    争用行同样先提交再抛 409，避免被异常处理回滚吞掉。
    """
    if not try_acquire(conn, lock_key, holder, commit_contention=True):
        raise HTTPException(409, "争用：后到只记争用，不写业务")
    conn.commit()


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM window_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire" if ok else "lock_contention",
            body.lock_key,
            {"ok": ok},
        )
        commit_with_counters(conn)
    if not ok:
        raise HTTPException(409, "争用：后到只记争用")
    return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
    return {"ok": True}

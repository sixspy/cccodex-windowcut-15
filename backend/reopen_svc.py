"""窗重开：作废重算落地（仪表同源册 2026-11-20 定版）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect, record_dash_deviation, refresh_dash_counters, write_audit
from locks import release, require_lock
from models import ReopenIn

router = APIRouter(tags=["reopen"])


@router.get("/api/reopen-ledger")
def list_reopen(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*, w.name AS window_name
               FROM reopen_ledger r
               JOIN windows w ON w.id=r.window_id
               ORDER BY r.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/windows/{wid}/reopen-stub")
def reopen_stub(wid: int, body: ReopenIn, authorization: AuthHeader = None):
    """作废重算：已关闭窗置回开启、世代+1、清封签，并与台账/审计/计数同事务。"""
    user = require_user(authorization)
    lock_key = f"window:{wid}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            if w["status"] != "已关闭":
                raise HTTPException(409, "仅已关闭窗可重开")
            # 同源册选定「作废重算」；另一口径不得混入生效。
            if body.mode != "作废重算":
                raise HTTPException(
                    400, "同源册定版：重开仅作废重算落地，增量补丁未启用"
                )
            now = datetime.now().isoformat(timespec="seconds")
            from_gen = int(w["generation"])
            to_gen = from_gen + 1
            cur = conn.execute(
                """INSERT INTO reopen_ledger(window_id,from_generation,to_generation,mode,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (wid, from_gen, to_gen, body.mode, "已重开", now),
            )
            conn.execute(
                """UPDATE windows SET status='开启', generation=?, seal_token=NULL
                   WHERE id=?""",
                (to_gen, wid),
            )
            write_audit(
                conn,
                user,
                "window_reopen",
                str(wid),
                {
                    "mode": body.mode,
                    "from_generation": from_gen,
                    "to_generation": to_gen,
                    "ledger_id": cur.lastrowid,
                },
            )
            refresh_dash_counters(conn)
        except HTTPException:
            conn.rollback()
            release(conn, lock_key, user)
            conn.commit()
            raise
        except Exception as exc:
            conn.rollback()
            release(conn, lock_key, user)
            record_dash_deviation(
                conn,
                user,
                str(wid),
                {"path": "乙", "stage": "window_reopen", "error": str(exc)},
            )
            raise
        release(conn, lock_key, user)
        conn.commit()
        return {
            "ok": True,
            "reopened": True,
            "mode": body.mode,
            "from_generation": from_gen,
            "to_generation": to_gen,
            "ledger_id": cur.lastrowid,
        }

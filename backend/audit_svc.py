"""审计与互证只读旁路。"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_user
from db import COUNTER_KEYS, counter_snapshot, connect, read_dash_counters

router = APIRouter(tags=["audit"])


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    limit = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/actions")
def audit_actions(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT action, COUNT(*) c FROM audit_trail
               GROUP BY action ORDER BY c DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/dashboard")
def dashboard(authorization: AuthHeader = None):
    """乙路径：只读 dash_counter 影子表，禁止在此聚合实表或缓存脏读。"""
    require_user(authorization)
    with connect() as conn:
        counts = read_dash_counters(conn)
        return {
            "open_windows": counts["open_windows"],
            "closed_windows": counts["closed_windows"],
            "late_bucket": counts["late_bucket"],
            "rejects": counts["rejects"],
            "holds": counts["holds"],
            "materializations": counts["materializations"],
            "contention": counts["contention"],
            "reopens": counts["reopens"],
            "source": "dash_counter",
        }


@router.get("/api/dashboard/same-source")
def dashboard_same_source(authorization: AuthHeader = None):
    """诊断面：影子计数与实表快照比对，仅暴露给审计视图，不参与展示计数。"""
    require_user(authorization)
    with connect() as conn:
        counters = read_dash_counters(conn)
        snapshot = counter_snapshot(conn)
        drift = {
            k: {"counter": counters[k], "live": snapshot[k]}
            for k in COUNTER_KEYS
            if counters[k] != snapshot[k]
        }
        return {"aligned": not drift, "drift": drift, "source": "dash_counter"}


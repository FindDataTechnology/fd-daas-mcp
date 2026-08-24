"""Wire cron-mcp schedules for the US-leaders Phase 1 MVP, from a clean
standalone process (the daas-mcp server's in-process _cron_call fails with
"Connection closed" due to an event-loop/stdio issue in the long-running
server context, but works fine standalone — verified).

Registers:
  • 12 fetch crons — one per pipeline item in `us-leaders-daily` (cron 30 4 * * 1-5 Asia/Shanghai).
    Task command: uv run --directory <repo>/mcp/daas-mcp python server.py --fetch-item <id>
  • 1 indicator-recompute cron — runs seed_us_leaders_indicators.py (cron 45 4 * * 1-5 Asia/Shanghai),
    15 min after the fetch, so observations refresh daily.

Idempotent: re-runnable. Uses the bridge's own register_cron_for_item + _cron_call.

    uv run --directory mcp/daas-mcp python register_us_leaders_cron.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
    load_dotenv(_THIS.parent / ".env", override=True)
except ImportError:
    pass

sys.path.insert(0, str(_THIS.parent))

from daas_database import get_database  # noqa: E402
from models import PipelineCollection, PipelineCollectionItem  # noqa: E402
from pipeline_tools import register_cron_for_item, _cron_call  # noqa: E402

COLLECTION = "us-leaders-daily"
REPO = str(_REPO_ROOT)
INDICATOR_TASK = "us_leaders_indicators_recompute"
INDICATOR_CRON = "45 4 * * 1-5"
INDICATOR_TZ = "Asia/Shanghai"
INDICATOR_CMD = f"uv run --directory {REPO}/mcp/daas-mcp python seed_us_leaders_indicators.py"


async def main() -> int:
    db = get_database()
    session = db.get_session()
    try:
        items = (
            session.query(PipelineCollectionItem)
            .join(PipelineCollection, PipelineCollectionItem.collection_id == PipelineCollection.id)
            .filter(PipelineCollection.name == COLLECTION)
            .order_by(PipelineCollectionItem.id)
            .all()
        )
    finally:
        session.close()

    print(f"Found {len(items)} items in collection '{COLLECTION}'")
    fetch_results = []
    for it in items:
        # detach so attribute access works after session close
        res = await register_cron_for_item(it)
        fetch_results.append((it.name, it.task_name, res))
        status = res.get("status")
        sid = res.get("schedule_id")
        err = res.get("error")
        print(f"  {it.name:14s} → {status}  schedule_id={sid}  {err or ''}")

    # Indicator-recompute cron (single task for all 72 indicators)
    print(f"\nWiring indicator-recompute cron '{INDICATOR_TASK}' ...")
    tasks = (await _cron_call("list_db_tasks", {})).get("tasks", []) or []
    if not any(t.get("name") == INDICATOR_TASK for t in tasks):
        r = await _cron_call("create_task", {
            "name": INDICATOR_TASK,
            "command": INDICATOR_CMD,
            "description": "Recompute all 72 US-leaders indicators (MA/RSI/volstd/high20) after the daily fetch",
            "timeout": 600,
        })
        print(f"  create_task: success={r.get('success') if isinstance(r, dict) else r}")
    else:
        await _cron_call("update_task", {"name": INDICATOR_TASK, "command": INDICATOR_CMD, "timeout": 600})
        print("  create_task: already exists, updated command")

    schedules = (await _cron_call("list_schedules", {})).get("schedules", []) or []
    mine = [s for s in schedules if s.get("task") == INDICATOR_TASK or s.get("name") == INDICATOR_TASK]
    if mine:
        sid = mine[0].get("id") or mine[0].get("schedule_id")
        if mine[0].get("cron") != INDICATOR_CRON or mine[0].get("timezone") != INDICATOR_TZ:
            await _cron_call("delete_schedule", {"schedule_id": sid})
            r = await _cron_call("create_schedule", {
                "name": INDICATOR_TASK, "cron": INDICATOR_CRON, "task": INDICATOR_TASK,
                "timezone": INDICATOR_TZ, "enabled": True,
            })
            print(f"  recreate schedule: {r.get('schedule_id') if isinstance(r, dict) else r}")
        else:
            print(f"  schedule already correct: {sid}")
    else:
        r = await _cron_call("create_schedule", {
            "name": INDICATOR_TASK, "cron": INDICATOR_CRON, "task": INDICATOR_TASK,
            "timezone": INDICATOR_TZ, "enabled": True,
        })
        print(f"  create_schedule: {r.get('schedule_id') if isinstance(r, dict) else r}")

    ok = sum(1 for _, _, r in fetch_results if r.get("status") == "ok")
    print(f"\nFetch crons wired: {ok}/{len(fetch_results)}")
    return 0 if ok == len(fetch_results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

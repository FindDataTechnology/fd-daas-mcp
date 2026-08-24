"""cron_tools tests: schedule CRUD round-trip, pause/resume enabled toggle,
run_now writes an execution record + list_executions filters by schedule, and
DB-task create/list/delete.

Calls the cron server tool handlers directly. APScheduler's import-time
`load_schedules()` (which starts a BackgroundScheduler thread) is suppressed by
patching `scheduler.load_schedules`/`shutdown_scheduler` to no-ops BEFORE
`server` is imported - mirroring the consolidation registry's `suppress=True`
path. The per-job functions (`add/remove/pause/resume_schedule_job`) are
monkeypatched on the `server` namespace per test so no `get_scheduler()` call
ever starts a thread. `run_schedule_now` is left REAL so `execute_task` writes a
real Execution row; the task it runs is a stub registered in the in-process
registry (no subprocess, no network).

Convention: schedules/tasks this module creates are prefixed `zz_test_` and torn
down by `_cleanup_cron()` in every test.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

_CRON_MCP = Path(__file__).resolve().parents[1] / "cron-mcp"
sys.path.insert(0, str(_CRON_MCP))

# Suppress cron's import-time scheduler start BEFORE importing `server`.
# `server.py` calls `init_db()` + `load_schedules()` at module top; load_schedules
# would call get_scheduler() -> BackgroundScheduler.start(). Patching it (and
# shutdown_scheduler, registered via atexit) to no-ops mirrors the registry's
# `suppress=True` path and keeps the suite thread-free.
import scheduler as cron_scheduler  # noqa: E402
cron_scheduler.load_schedules = lambda *a, **k: None  # type: ignore[assignment]
cron_scheduler.shutdown_scheduler = lambda *a, **k: None  # type: ignore[assignment]
import server as cron_server  # noqa: E402  (runs init_db() + no-op load_schedules)
import database as cron_database  # noqa: E402
import registry as cron_registry  # noqa: E402
from models import Execution, Schedule, Task  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _cleanup_cron() -> None:
    sess = cron_database.get_session()
    try:
        sess.query(Execution).filter(
            Execution.schedule_id.in_(
                sess.query(Schedule.id).filter(Schedule.name.like("zz_test_%"))
            )
        ).delete(synchronize_session=False)
        sess.query(Schedule).filter(Schedule.name.like("zz_test_%")).delete()
        sess.query(Task).filter(Task.name.like("zz_test_%")).delete()
        sess.commit()
    finally:
        sess.close()
    # Remove any stub task registered into the in-process registry.
    cron_registry.TASKS.pop("zz_test_runtask", None)


@pytest.fixture
def stub_scheduler_jobs(monkeypatch):
    """No-op the per-job scheduler functions on the `server` namespace so
    create/delete/pause/resume never call get_scheduler() (no thread started).

    `run_schedule_now` is deliberately left real so the run_now test exercises
    a real execute_task() + Execution-row write against a stub registry task.
    """
    for fn in (
        "add_schedule_job",
        "remove_schedule_job",
        "pause_schedule_job",
        "resume_schedule_job",
    ):
        monkeypatch.setattr(cron_server, fn, lambda *a, **k: None)


def test_schedule_crud_round_trip(stub_scheduler_jobs):
    _cleanup_cron()
    res = _run(
        cron_server.create_schedule(
            name="zz_test_crud", cron="*/5 * * * *", task="news_summary", enabled=True
        )
    )
    assert res.get("success") is True, res
    sid = res["schedule_id"]
    assert res["name"] == "zz_test_crud"
    assert res["cron"] == "*/5 * * * *"
    assert res["enabled"] is True

    listed = _run(cron_server.list_schedules())
    assert sid in [s["id"] for s in listed["schedules"]]

    got = _run(cron_server.get_schedule(sid))
    assert got["success"] is True
    assert got["schedule"]["name"] == "zz_test_crud"
    assert got["schedule"]["task"] == "news_summary"

    dele = _run(cron_server.delete_schedule(sid))
    assert dele.get("success") is True
    assert dele["schedule_id"] == sid

    listed2 = _run(cron_server.list_schedules())
    assert sid not in [s["id"] for s in listed2["schedules"]]

    # No APScheduler thread was started (load_schedules + per-job funcs stubbed).
    threads = [
        t.name for t in threading.enumerate()
        if "apscheduler" in t.name.lower() or "scheduler" in t.name.lower()
    ]
    assert not threads, f"scheduler thread started: {threads}"
    _cleanup_cron()


def test_pause_resume_toggles_enabled(stub_scheduler_jobs):
    _cleanup_cron()
    res = _run(
        cron_server.create_schedule(
            name="zz_test_pause", cron="*/5 * * * *", task="news_summary", enabled=True
        )
    )
    sid = res["schedule_id"]

    paused = _run(cron_server.pause_schedule(sid))
    assert paused.get("success") is True
    assert paused["enabled"] is False
    sess = cron_database.get_session()
    try:
        assert sess.get(Schedule, sid).enabled == 0
    finally:
        sess.close()

    resumed = _run(cron_server.resume_schedule(sid))
    assert resumed.get("success") is True
    assert resumed["enabled"] is True
    sess = cron_database.get_session()
    try:
        assert sess.get(Schedule, sid).enabled == 1
    finally:
        sess.close()
    _cleanup_cron()


def test_run_now_records_execution_and_list_filters(stub_scheduler_jobs):
    _cleanup_cron()
    # Register a stub task in the in-process registry (no subprocess, no network).
    cron_registry.register_task("zz_test_runtask", lambda: "zz-out")

    res = _run(
        cron_server.create_schedule(
            name="zz_test_run", cron="*/5 * * * *", task="zz_test_runtask", enabled=True
        )
    )
    sid = res["schedule_id"]

    ran = _run(cron_server.run_now(sid))
    assert ran.get("success") is True, ran

    # An Execution row was written (status=completed, output from the stub task).
    ev = _run(cron_server.list_executions(schedule_id=sid))
    assert ev["count"] >= 1, ev
    row = ev["executions"][0]
    assert row["schedule_id"] == sid
    assert row["status"] == "completed"
    assert row["output"] == "zz-out"

    # list_executions without a filter still includes it; a different filter excludes it.
    all_ev = _run(cron_server.list_executions(limit=200))
    assert any(e["schedule_id"] == sid for e in all_ev["executions"])
    other = _run(cron_server.list_executions(schedule_id="does-not-exist"))
    assert other["count"] == 0

    # schedule.last_run_at was advanced.
    sess = cron_database.get_session()
    try:
        assert sess.get(Schedule, sid).last_run_at is not None
    finally:
        sess.close()
    _cleanup_cron()


def test_create_task_list_db_tasks_and_delete_task():
    _cleanup_cron()
    res = _run(
        cron_server.create_task(
            name="zz_test_task", command="echo hello", description="d", timeout=5
        )
    )
    assert res.get("success") is True, res
    assert res["name"] == "zz_test_task"
    assert res["command"] == "echo hello"

    listed = _run(cron_server.list_db_tasks())
    assert "zz_test_task" in [t["name"] for t in listed["tasks"]]

    dele = _run(cron_server.delete_task("zz_test_task"))
    assert dele.get("success") is True
    assert dele["name"] == "zz_test_task"

    listed2 = _run(cron_server.list_db_tasks())
    assert "zz_test_task" not in [t["name"] for t in listed2["tasks"]]
    _cleanup_cron()

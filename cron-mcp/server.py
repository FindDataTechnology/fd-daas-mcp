"""Agent Scheduler MCP Server — AI-agent-driven cron scheduling with SQLite + APScheduler.

Run: fastmcp run server.py
"""

import atexit
import json
import logging
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # mcp/
load_dotenv(ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=True)

from fastmcp import FastMCP

from database import get_session, init_db
from models import Execution, Schedule, Task
from registry import list_tasks as registry_list_tasks
from scheduler import (
    add_schedule_job,
    load_schedules,
    pause_schedule_job,
    remove_schedule_job,
    resume_schedule_job,
    run_schedule_now,
    shutdown_scheduler,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("cron-mcp")

# ---------------------------------------------------------------------------
# FastMCP Application
# ---------------------------------------------------------------------------
app = FastMCP(name="cron-mcp")

# ---------------------------------------------------------------------------
# Lifecycle — init DB and load schedules at startup, shutdown gracefully
# ---------------------------------------------------------------------------
init_db()
load_schedules()
atexit.register(shutdown_scheduler)
logger.info("Agent Scheduler MCP Server ready")


# ---------------------------------------------------------------------------
# Tools — FastMCP infers schemas from type annotations + docstrings
# ---------------------------------------------------------------------------

@app.tool
async def create_schedule(
    name: str,
    cron: str,
    task: str,
    agent: Optional[str] = None,
    prompt: Optional[str] = None,
    timezone: str = "UTC",
    enabled: bool = True,
) -> dict:
    """Create a new schedule with a cron expression and task."""
    session = get_session()
    try:
        schedule = Schedule(
            name=name,
            cron_expr=cron,
            task_name=task,
            agent=agent,
            prompt=prompt,
            timezone=timezone,
            enabled=1 if enabled else 0,
        )
        session.add(schedule)
        session.commit()

        if schedule.enabled:
            add_schedule_job(schedule)
            session.commit()

        logger.info("Created schedule: %s (id=%s)", schedule.name, schedule.id)
        return {
            "success": True,
            "schedule_id": schedule.id,
            "name": schedule.name,
            "cron": schedule.cron_expr,
            "task": schedule.task_name,
            "agent": schedule.agent,
            "enabled": bool(schedule.enabled),
        }
    except Exception as e:
        session.rollback()
        logger.exception("Failed to create schedule")
        return {"success": False, "error": str(e)}
    finally:
        session.close()


@app.tool
async def list_schedules() -> dict:
    """List all schedules."""
    session = get_session()
    try:
        schedules = session.query(Schedule).order_by(Schedule.created_at.desc()).all()
        result = []
        for s in schedules:
            result.append({
                "id": s.id,
                "name": s.name,
                "cron": s.cron_expr,
                "task": s.task_name,
                "agent": s.agent,
                "enabled": bool(s.enabled),
                "timezone": s.timezone,
                "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            })
        return {"schedules": result, "count": len(result)}
    finally:
        session.close()


@app.tool
async def get_schedule(schedule_id: str) -> dict:
    """Get details for a single schedule."""
    session = get_session()
    try:
        s = session.query(Schedule).filter(Schedule.id == schedule_id).first()
        if s is None:
            return {"success": False, "error": f"Schedule '{schedule_id}' not found"}
        return {
            "success": True,
            "schedule": {
                "id": s.id,
                "name": s.name,
                "cron": s.cron_expr,
                "task": s.task_name,
                "agent": s.agent,
                "prompt": s.prompt,
                "enabled": bool(s.enabled),
                "timezone": s.timezone,
                "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            },
        }
    finally:
        session.close()


@app.tool
async def delete_schedule(schedule_id: str) -> dict:
    """Delete a schedule permanently."""
    session = get_session()
    try:
        s = session.query(Schedule).filter(Schedule.id == schedule_id).first()
        if s is None:
            return {"success": False, "error": f"Schedule '{schedule_id}' not found"}

        remove_schedule_job(schedule_id)
        session.delete(s)
        session.commit()

        logger.info("Deleted schedule: %s", schedule_id)
        return {"success": True, "schedule_id": schedule_id, "name": s.name}
    except Exception as e:
        session.rollback()
        return {"success": False, "error": str(e)}
    finally:
        session.close()


@app.tool
async def pause_schedule(schedule_id: str) -> dict:
    """Pause a schedule (disable without deleting)."""
    session = get_session()
    try:
        s = session.query(Schedule).filter(Schedule.id == schedule_id).first()
        if s is None:
            return {"success": False, "error": f"Schedule '{schedule_id}' not found"}

        pause_schedule_job(schedule_id)
        s.enabled = 0
        session.commit()

        logger.info("Paused schedule: %s", schedule_id)
        return {"success": True, "schedule_id": schedule_id, "name": s.name, "enabled": False}
    except Exception as e:
        session.rollback()
        return {"success": False, "error": str(e)}
    finally:
        session.close()


@app.tool
async def resume_schedule(schedule_id: str) -> dict:
    """Resume a paused schedule."""
    session = get_session()
    try:
        s = session.query(Schedule).filter(Schedule.id == schedule_id).first()
        if s is None:
            return {"success": False, "error": f"Schedule '{schedule_id}' not found"}

        resume_schedule_job(schedule_id)
        s.enabled = 1
        session.commit()

        logger.info("Resumed schedule: %s", schedule_id)
        return {"success": True, "schedule_id": schedule_id, "name": s.name, "enabled": True}
    except Exception as e:
        session.rollback()
        return {"success": False, "error": str(e)}
    finally:
        session.close()


@app.tool
async def run_now(schedule_id: str) -> dict:
    """Execute a scheduled task immediately (one-shot)."""
    session = get_session()
    try:
        s = session.query(Schedule).filter(Schedule.id == schedule_id).first()
        if s is None:
            return {"success": False, "error": f"Schedule '{schedule_id}' not found"}

        run_schedule_now(schedule_id)
        return {"success": True, "schedule_id": schedule_id, "name": s.name, "message": "Task executed"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        session.close()


@app.tool
async def list_executions(schedule_id: Optional[str] = None, limit: int = 50) -> dict:
    """List execution history, optionally filtered by schedule_id."""
    session = get_session()
    try:
        q = session.query(Execution).order_by(Execution.started_at.desc())
        if schedule_id:
            q = q.filter(Execution.schedule_id == schedule_id)
        executions = q.limit(limit).all()

        result = []
        for e in executions:
            result.append({
                "id": e.id,
                "schedule_id": e.schedule_id,
                "status": e.status,
                "started_at": e.started_at.isoformat() if e.started_at else None,
                "finished_at": e.finished_at.isoformat() if e.finished_at else None,
                "output": e.output,
            })
        return {"executions": result, "count": len(result)}
    finally:
        session.close()


@app.tool
async def list_tasks() -> dict:
    """List available task types in the registry."""
    tasks = registry_list_tasks()
    return {"tasks": [{"name": k, "description": v} for k, v in tasks.items()]}


@app.tool
async def create_task(
    name: str,
    command: str,
    description: str = "",
    timeout: int = 60,
) -> dict:
    """Register a new task in the database. The command is a shell command or script path.

    Example: create_task(name="fetch_mofcom", command="python3 scripts/fetch_mofcom_news.py --json")
    """
    session = get_session()
    try:
        existing = session.query(Task).filter(Task.name == name).first()
        if existing:
            return {"success": False, "error": f"Task '{name}' already exists (id={existing.id})"}

        task = Task(name=name, command=command, description=description, timeout=timeout)
        session.add(task)
        session.commit()
        logger.info("Created DB task: %s (id=%s)", task.name, task.id)
        return {"success": True, "task_id": task.id, "name": task.name, "command": task.command}
    except Exception as e:
        session.rollback()
        return {"success": False, "error": str(e)}
    finally:
        session.close()


@app.tool
async def delete_task(name: str) -> dict:
    """Delete a task from the database by name."""
    session = get_session()
    try:
        task = session.query(Task).filter(Task.name == name).first()
        if task is None:
            return {"success": False, "error": f"Task '{name}' not found"}
        session.delete(task)
        session.commit()
        logger.info("Deleted DB task: %s", name)
        return {"success": True, "name": name}
    except Exception as e:
        session.rollback()
        return {"success": False, "error": str(e)}
    finally:
        session.close()


@app.tool
async def list_db_tasks() -> dict:
    """List all tasks stored in the database (user-defined, no code changes needed)."""
    session = get_session()
    try:
        tasks = session.query(Task).order_by(Task.created_at.desc()).all()
        result = [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description or "",
                "command": t.command,
                "timeout": t.timeout,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in tasks
        ]
        return {"tasks": result, "count": len(result)}
    finally:
        session.close()


@app.tool
async def update_task(
    name: str,
    command: str = "",
    description: str = "",
    timeout: int = 0,
) -> dict:
    """Update an existing DB task's command, description, or timeout. Only provided fields are updated."""
    session = get_session()
    try:
        task = session.query(Task).filter(Task.name == name).first()
        if task is None:
            return {"success": False, "error": f"Task '{name}' not found"}

        if command:
            task.command = command
        if description:
            task.description = description
        if timeout:
            task.timeout = timeout
        session.commit()
        logger.info("Updated DB task: %s", name)
        return {"success": True, "name": name}
    except Exception as e:
        session.rollback()
        return {"success": False, "error": str(e)}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(transport="stdio", show_banner=False)

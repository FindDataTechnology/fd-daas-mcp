"""Agent runner — executes scheduled tasks and records results."""

import logging
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

from database import get_session
from models import Execution, Schedule, Task
from registry import get_task

logger = logging.getLogger(__name__)


def _run_db_task(task: Task) -> str:
    """Execute a DB-backed task as a subprocess. Returns stdout or error message."""
    logger.info("Running DB task '%s': %s", task.name, task.command)
    try:
        result = subprocess.run(
            task.command,
            capture_output=True, text=True, timeout=task.timeout, shell=True,
        )
        if result.returncode != 0:
            logger.error("DB task '%s' failed (rc=%d): %s", task.name, result.returncode, result.stderr)
            return f"FAILED (rc={result.returncode}): {result.stderr.strip()}"
        logger.info("DB task '%s' OK, %d chars", task.name, len(result.stdout))
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.error("DB task '%s' timed out after %ds", task.name, task.timeout)
        return f"TIMEOUT after {task.timeout}s"
    except Exception as e:
        logger.exception("DB task '%s' error", task.name)
        return f"ERROR: {e}"


def _resolve_task(task_name: str):
    """Resolve a task: DB first, then registry. Returns (callable_or_None, db_task_or_None)."""
    session = get_session()
    try:
        db_task = session.query(Task).filter(Task.name == task_name).first()
        if db_task is not None:
            return (lambda: _run_db_task(db_task), db_task)
    finally:
        session.close()

    try:
        return (get_task(task_name), None)
    except KeyError:
        return (None, None)


def execute_task(schedule_id: str) -> None:
    """Execute a scheduled task by its schedule_id.

    This is the entry point called by APScheduler when a job fires.
    It:
      1. Loads the schedule from DB
      2. Creates an execution record (status=pending)
      3. Resolves the task (DB tasks first, then registry)
      4. Runs the task
      5. Updates the execution record with results
      6. Updates schedule last_run_at
    """
    session = get_session()
    execution: Optional[Execution] = None

    try:
        schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
        if schedule is None:
            logger.error("Schedule %s not found in database", schedule_id)
            return

        if not schedule.enabled:
            logger.info("Schedule %s is disabled, skipping", schedule_id)
            return

        # Create execution record
        execution = Execution(
            schedule_id=schedule_id,
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        session.add(execution)
        session.commit()

        logger.info("Executing schedule '%s' (task=%s, agent=%s)", schedule.name, schedule.task_name, schedule.agent)

        # Resolve and run the task
        task_func, db_task = _resolve_task(schedule.task_name)
        if task_func is None:
            raise KeyError(f"Task '{schedule.task_name}' not found in DB or registry")

        # If an agent is specified, log it — in the MVP the task function itself
        # handles agent invocation. Future: launch subprocess/claude agent here.
        if schedule.agent:
            logger.info("Agent mode: %s with prompt: %s", schedule.agent, schedule.prompt)

        result = task_func()

        # Mark execution as completed
        execution.status = "completed"
        execution.output = result
        execution.finished_at = datetime.now(timezone.utc)

        # Update schedule timestamps
        schedule.last_run_at = datetime.now(timezone.utc)

        session.commit()
        logger.info("Schedule '%s' completed successfully", schedule.name)

    except Exception as e:
        logger.exception("Schedule %s failed: %s", schedule_id, e)
        if execution:
            execution.status = "failed"
            execution.output = str(e)
            execution.finished_at = datetime.now(timezone.utc)
            session.commit()
    finally:
        session.close()

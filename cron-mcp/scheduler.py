"""APScheduler lifecycle management — load, register, and control scheduled jobs."""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError

from agent_runner import execute_task
from database import get_session
from models import Schedule

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    """Get or create the global background scheduler."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
        _scheduler.start()
        logger.info("APScheduler started")
    return _scheduler


def load_schedules() -> None:
    """Load all enabled schedules from the database and register them as APScheduler jobs."""
    scheduler = get_scheduler()
    session = get_session()

    try:
        schedules = session.query(Schedule).filter(Schedule.enabled == 1).all()
        loaded = 0

        for s in schedules:
            try:
                job_id = f"schedule:{s.id}"
                if scheduler.get_job(job_id):
                    logger.debug("Job %s already registered, skipping", job_id)
                    continue

                scheduler.add_job(
                    execute_task,
                    trigger=CronTrigger.from_crontab(s.cron_expr, timezone=s.timezone),
                    id=job_id,
                    args=[s.id],
                    replace_existing=True,
                    name=s.name,
                )

                # Update next_run_at
                job = scheduler.get_job(job_id)
                if job and job.next_run_time:
                    s.next_run_at = job.next_run_time

                loaded += 1
                logger.info("Loaded schedule: %s (cron=%s, task=%s)", s.name, s.cron_expr, s.task_name)

            except Exception as e:
                logger.error("Failed to load schedule %s: %s", s.id, e)

        session.commit()
        logger.info("Loaded %d/%d schedules from database", loaded, len(schedules))

    finally:
        session.close()


def add_schedule_job(schedule: Schedule) -> None:
    """Register a single schedule as an APScheduler job."""
    scheduler = get_scheduler()
    job_id = f"schedule:{schedule.id}"

    scheduler.add_job(
        execute_task,
        trigger=CronTrigger.from_crontab(schedule.cron_expr, timezone=schedule.timezone),
        id=job_id,
        args=[schedule.id],
        replace_existing=True,
        name=schedule.name,
    )

    job = scheduler.get_job(job_id)
    if job and job.next_run_time:
        schedule.next_run_at = job.next_run_time

    logger.info("Registered job: %s", job_id)


def remove_schedule_job(schedule_id: str) -> None:
    """Remove a schedule's APScheduler job."""
    scheduler = get_scheduler()
    job_id = f"schedule:{schedule_id}"
    try:
        scheduler.remove_job(job_id)
        logger.info("Removed job: %s", job_id)
    except JobLookupError:
        logger.warning("Job %s not found in scheduler", job_id)


def pause_schedule_job(schedule_id: str) -> None:
    """Pause a schedule's APScheduler job."""
    scheduler = get_scheduler()
    job_id = f"schedule:{schedule_id}"
    try:
        scheduler.pause_job(job_id)
        logger.info("Paused job: %s", job_id)
    except JobLookupError:
        logger.warning("Job %s not found in scheduler", job_id)


def resume_schedule_job(schedule_id: str) -> None:
    """Resume a schedule's APScheduler job."""
    scheduler = get_scheduler()
    job_id = f"schedule:{schedule_id}"
    try:
        scheduler.resume_job(job_id)
        logger.info("Resumed job: %s", job_id)
    except JobLookupError:
        logger.warning("Job %s not found in scheduler", job_id)


def run_schedule_now(schedule_id: str) -> str:
    """Execute a schedule's task immediately (ad-hoc, outside cron)."""
    logger.info("Running schedule %s immediately", schedule_id)
    execute_task(schedule_id)
    return f"Task for schedule {schedule_id} executed"


def shutdown_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("APScheduler shut down")

"""Task registry mapping task names to callables."""

import logging
from typing import Any, Callable, Dict

from tasks import run_backup, run_news_summary, run_weekly_report

logger = logging.getLogger(__name__)

# Registry of built-in task functions
TASKS: Dict[str, Callable[[], str]] = {
    "news_summary": run_news_summary,
    "weekly_report": run_weekly_report,
    "backup": run_backup,
}


def register_task(name: str, func: Callable[[], str]) -> None:
    """Register a new task function at runtime."""
    TASKS[name] = func
    logger.info("Registered task: %s", name)


def get_task(name: str) -> Callable[[], str]:
    """Look up a task by name. Raises KeyError if not found."""
    if name not in TASKS:
        raise KeyError(f"Task '{name}' not found in registry. Available: {list(TASKS.keys())}")
    return TASKS[name]


def list_tasks() -> Dict[str, str]:
    """Return a summary of all registered tasks."""
    return {name: func.__doc__ or "(no description)" for name, func in TASKS.items()}

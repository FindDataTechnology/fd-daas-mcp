"""Built-in task callables for the cron scheduler."""

import logging

logger = logging.getLogger(__name__)


def run_news_summary() -> str:
    """Fetch and summarize today's news."""
    logger.info("Running news summary task")
    return "news_summary completed"


def run_weekly_report() -> str:
    """Generate a weekly report."""
    logger.info("Running weekly report task")
    return "weekly_report completed"


def run_backup() -> str:
    """Run a data backup."""
    logger.info("Running backup task")
    return "backup completed"

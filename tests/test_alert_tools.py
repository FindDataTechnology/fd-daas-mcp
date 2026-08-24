"""alert_tools tests: rule CRUD round-trip, run_rule fire + event write (with
messaging send stubbed), run_rule cooldown (no refire within window) + no-fire
(condition false writes no event).

Calls alert_tools functions directly. alerts-mcp/ is added to sys.path for the
imports, then removed. The alert series a rule evaluates is seeded into the
throwaway DB's `observations` table (created by Base.metadata.create_all).
Notification dispatch (`notifiers.registry.send`, imported into engine as
`_channel_send`) is monkeypatched to a capturer so no network call is made.

Convention: every rule/series this module creates is prefixed `zz_test_` and
torn down by `_cleanup_alerts()` in every test (and on failure).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

_ALERTS_MCP = Path(__file__).resolve().parents[1] / "alerts-mcp"
sys.path.insert(0, str(_ALERTS_MCP))
from alert_database import get_db  # noqa: E402
from models import AlertEvent, AlertRule  # noqa: E402
import alert_tools  # noqa: E402
import engine  # noqa: E402
import expressions  # noqa: F401,E402  # cached in sys.modules so engine.evaluate_rule's late `import expressions` resolves
# NOTE: alerts-mcp/ is left on sys.path (not removed, unlike test_rule_tools)
# because engine.evaluate_rule late-imports `expressions` at call time.


def _cleanup_alerts() -> None:
    sess = get_db().get_session()
    try:
        sess.query(AlertEvent).filter(
            AlertEvent.rule_id.in_(
                sess.query(AlertRule.id).filter(AlertRule.name.like("zz_test_%"))
            )
        ).delete(synchronize_session=False)
        sess.query(AlertRule).filter(AlertRule.name.like("zz_test_%")).delete()
        sess.commit()
    finally:
        sess.close()
    # Remove the seeded observations rows this module wrote.
    with get_db().engine.connect() as conn:
        conn.execute(text("DELETE FROM observations WHERE source LIKE 'zz_test_%'"))
        conn.commit()


def _seed_observations(source: str, rows: list[tuple[str, str]]) -> None:
    """Insert (date, value) rows into observations under `source`/indicator 'close'.

    `rows` is ordered oldest-first; the engine reads newest-first by date DESC.
    """
    sess = get_db().get_session()
    try:
        for date, value in rows:
            sess.add(
                _ObsRow(source, date, value)
            )
        sess.commit()
    finally:
        sess.close()


def _ObsRow(source, date, value):
    """Build an Observation ORM row without importing the model at module top
    (keeps the import block uniform across the 5 new modules)."""
    from models import Observation

    return Observation(
        source=source,
        function_name="zz_test_fn",
        indicator="close",
        date=date,
        value=value,
    )


def test_alert_rule_crud_round_trip():
    _cleanup_alerts()
    res = alert_tools.create_alert_rule(
        name="zz_test_crud",
        condition="latest > 70",
        channels=["telegram"],
        source_table="observations",
        series_filter_json={"source": "zz_test_crud"},
    )
    assert not res.get("error"), res
    assert res["name"] == "zz_test_crud"
    assert res["condition"] == "latest > 70"
    assert res["enabled"] is True

    listed = alert_tools.list_alert_rules()
    assert "zz_test_crud" in [r["name"] for r in listed["rules"]]

    got = alert_tools.get_alert_rule("zz_test_crud")
    assert got["name"] == "zz_test_crud"

    upd = alert_tools.update_alert_rule(name="zz_test_crud", condition="latest > 80")
    assert upd["condition"] == "latest > 80"

    dele = alert_tools.delete_alert_rule("zz_test_crud")
    assert dele.get("deleted") == "zz_test_crud"
    assert dele.get("events_cascaded") is True
    # Gone from list.
    listed2 = alert_tools.list_alert_rules()
    assert "zz_test_crud" not in [r["name"] for r in listed2["rules"]]
    # get on a deleted rule surfaces an error.
    assert "error" in alert_tools.get_alert_rule("zz_test_crud")
    _cleanup_alerts()


def test_run_rule_fires_and_records_event(monkeypatch):
    _cleanup_alerts()
    _seed_observations("zz_test_fire", [("2024-01-01", "60"), ("2024-01-02", "80")])

    sent: list[tuple] = []

    def _fake_send(channel, message, ctx):
        sent.append((channel, message))
        return {"ok": True, "channel": channel}

    monkeypatch.setattr(engine, "_channel_send", _fake_send)

    alert_tools.create_alert_rule(
        name="zz_test_fire",
        condition="latest > 70",
        channels=["telegram"],
        source_table="observations",
        series_filter_json={"source": "zz_test_fire"},
        cooldown_seconds=300,
    )

    out = alert_tools.run_rule("zz_test_fire")
    assert out.get("fired") is True, out
    assert out["latest"] == 80.0
    assert out["ok_channels"] == 1 and out["total_channels"] == 1
    # The stub captured the dispatch with a rendered message.
    assert sent and sent[0][0] == "telegram"
    assert "80" in sent[0][1]

    # An alert_events row was written and is returned by list_events.
    events = alert_tools.list_events(rule_name="zz_test_fire")
    assert events["events"], events
    ev = events["events"][0]
    assert ev["channels_results"][0]["ok"] is True

    # The rule's last_fired_at / last_state were persisted.
    rule = alert_tools.get_alert_rule("zz_test_fire")
    assert rule["last_state"] is True
    assert rule["last_fired_at"] is not None
    _cleanup_alerts()


def test_run_rule_respects_cooldown(monkeypatch):
    _cleanup_alerts()
    _seed_observations("zz_test_cool", [("2024-01-01", "60"), ("2024-01-02", "80")])

    monkeypatch.setattr(
        engine, "_channel_send", lambda ch, msg, ctx: {"ok": True, "channel": ch}
    )
    alert_tools.create_alert_rule(
        name="zz_test_cool",
        condition="latest > 70",
        channels=["telegram"],
        source_table="observations",
        series_filter_json={"source": "zz_test_cool"},
        cooldown_seconds=300,
    )

    first = alert_tools.run_rule("zz_test_cool")
    assert first.get("fired") is True, first

    # Second run is within the 300s cooldown -> no refire, no new event.
    second = alert_tools.run_rule("zz_test_cool")
    assert second.get("fired") is False, second
    assert second.get("reason") == "cooldown"

    events = alert_tools.list_events(rule_name="zz_test_cool")
    assert len(events["events"]) == 1, events
    _cleanup_alerts()


def test_run_rule_no_fire_writes_no_event(monkeypatch):
    _cleanup_alerts()
    # latest = 50, condition `latest > 70` is false.
    _seed_observations("zz_test_nofire", [("2024-01-01", "60"), ("2024-01-02", "50")])

    monkeypatch.setattr(
        engine, "_channel_send", lambda ch, msg, ctx: {"ok": True, "channel": ch}
    )
    alert_tools.create_alert_rule(
        name="zz_test_nofire",
        condition="latest > 70",
        channels=["telegram"],
        source_table="observations",
        series_filter_json={"source": "zz_test_nofire"},
    )

    out = alert_tools.run_rule("zz_test_nofire")
    assert out.get("fired") is False, out
    assert out.get("reason") == "condition false"

    events = alert_tools.list_events(rule_name="zz_test_nofire")
    assert events["events"] == [], events
    # last_state flipped to False (so on_change could detect the next rise).
    rule = alert_tools.get_alert_rule("zz_test_nofire")
    assert rule["last_state"] is False
    _cleanup_alerts()

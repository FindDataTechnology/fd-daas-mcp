"""Self-check for alerts-mcp — exercises the full path with no network and no
real social posts.

Run:  uv run --directory mcp/alerts-mcp python selfcheck.py

Uses a temp DB. Stubs the dispatch path (engine._channel_send) so no HTTP is
made and no real messages are sent. Verifies:
  - rule CRUD + identifier/injection guard
  - condition DSL: thresholds, crossings, pct_change, and malicious-expression
    rejection (no eval/exec)
  - on_change fire mode: false→true→false→true cycle
  - every_match + cooldown gating
  - dispatch fan-out with one failing channel (fault isolation)
  - alert_events row insertion + rule-state update
  - list_channels secret-redaction (no credential leakage)
  - Twitter OAuth 1.0a signing vs an oauthlib-verified vector
  - CLI --run-rule / --run-all exit without starting stdio

Passes with no ALERTS_* env vars set.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# isolate: temp DB before importing server
_TMP = Path(tempfile.gettempdir()) / "alerts_mcp_selfcheck.db"
if _TMP.exists():
    _TMP.unlink()
os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_TMP}"

sys.path.insert(0, str(Path(__file__).resolve().parent))
# make mcp/models importable (server.py does this too, but alert_database imports
# `from models import ...` before server is imported)
_MODELS = Path(__file__).resolve().parent.parent / "models"
if str(_MODELS) not in sys.path:
    sys.path.insert(0, str(_MODELS))

import alert_database as adb  # noqa: E402
import expressions as E  # noqa: E402
import engine  # noqa: E402
import server  # noqa: E402
from notifiers import list_channels  # noqa: E402
from notifiers.twitter import sign as twitter_sign  # noqa: E402

# Clear any ALERTS_* leaked from root .env so the "no env" baseline holds.
for _k in list(os.environ):
    if _k.startswith("ALERTS_"):
        del os.environ[_k]

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


# ── stub the dispatch path: no network, no real posts ───────────
_calls: list[tuple[str, str]] = []


def _stub_send(channel: str, message: str, ctx: dict) -> dict:
    _calls.append((channel, message))
    if channel == "slack":
        return {"ok": False, "channel": channel, "error": "stub failure (slack)"}
    return {"ok": True, "channel": channel}


engine._channel_send = _stub_send

db = adb.get_db()

# ── 1. list_channels: 7 channels, all unconfigured with no env ───
ch = list_channels()
check("list_channels returns 7 channels", len(ch) == 7, str([c["name"] for c in ch]))
check(
    "list_channels names correct",
    {c["name"] for c in ch}
    == {"telegram", "discord", "slack", "twitter", "dingtalk", "feishu", "wecowork"},
)
check("all channels unconfigured with no env", all(not c["configured"] for c in ch), str(ch))

# ── 2. condition DSL: thresholds, crossings, pct_change ──────────
ctx = {"latest": 75.0, "prev": 68.0, "series": [75.0, 68.0, 70.0, 72.0, 69.0, 71.0]}
check("dsl latest>70 true", E.evaluate("latest > 70", ctx) is True)
check("dsl latest>80 false", E.evaluate("latest > 80", ctx) is False)
check("dsl crosses_above(70) true", E.evaluate("crosses_above(70)", ctx) is True)
check("dsl crosses_below(70) false", E.evaluate("crosses_below(70)", ctx) is False)
check("dsl pct_change(5)>0.05 true", E.evaluate("pct_change(5) > 0.05", ctx) is True)
check("dsl and/or", E.evaluate("latest > 70 and prev <= 70", ctx) is True)
check("dsl not", E.evaluate("not (latest < 70)", ctx) is True)

# ── 3. malicious expressions rejected (no eval/exec) ─────────────
for bad in [
    "__import__('os').system('rm -rf /')",
    "latest.__class__",
    "().__class__.__bases__",
    'os.system("x")',
    "latest[0]",
    "[x for x in range(10)]",
    'getattr(latest, "real")',
    "lambda: 1",
    "open('/etc/passwd').read()",
]:
    try:
        E.evaluate(bad, ctx)
        check(f"blocked malicious: {bad[:30]}", False, "was NOT blocked")
    except E.ExpressionError:
        check(f"blocked malicious: {bad[:30]}", True)

# ── 4. Twitter OAuth 1.0a signing vs oauthlib-verified vector ────
# Vector verified against oauthlib's Client (see design D5). Inputs:
#   POST https://api.twitter.com/1.1/statuses/update.json
#   body param status="Hello World" + oauth_* (incl oauth_version=1.0)
#   consumer_secret="cs", token_secret="ts"
tw_params = [
    ("status", "Hello World"),
    ("oauth_consumer_key", "ck"),
    ("oauth_nonce", "abc123"),
    ("oauth_signature_method", "HMAC-SHA1"),
    ("oauth_timestamp", "1700000000"),
    ("oauth_token", "tk"),
    ("oauth_version", "1.0"),
]
tw_sig = twitter_sign(
    "POST", "https://api.twitter.com/1.1/statuses/update.json", tw_params, "cs", "ts"
)
check(
    "twitter OAuth signature matches oracle vector",
    tw_sig == "h/NEWvkp64JcbpY21mqKU9KBmvU=",
    f"got {tw_sig}",
)

# ── 5. build a scraw fixture + rule CRUD ─────────────────────────
with db.engine.begin() as conn:
    conn.execute(adb.text(
        "CREATE TABLE scraw_alerts_test (id INTEGER PRIMARY KEY, date TEXT, value REAL)"
    ))
    conn.execute(
        adb.text("INSERT INTO scraw_alerts_test (date, value) VALUES (:d,:v)"),
        [{"d": "d1", "v": 68.0}, {"d": "d2", "v": 75.0}],
    )

series = db.list_series()
check("list_series finds scraw_alerts_test", any(s.get("table") == "scraw_alerts_test" for s in series), str(series))
latest = db.get_series_latest("scraw_alerts_test", {}, "date", "value", limit=5)
check("get_series_latest newest-first", latest[0]["date"] == "d2" and latest[0]["value"] == 75.0, str(latest))

r = db.create_rule(
    name="onchange",
    condition="crosses_above(70)",
    channels=["telegram"],
    source_table="scraw_alerts_test",
    date_column="date",
    value_column="value",
    fire_mode="on_change",
    message_template="$rule_name fired: $indicator=$latest (prev $prev)",
)
check("create_rule ok", r["name"] == "onchange" and r["fire_mode"] == "on_change", str(r))
check("list_rules includes onchange", any(x["name"] == "onchange" for x in db.list_rules()))
check("get_rule ok", db.get_rule("onchange")["condition"] == "crosses_above(70)")

# create_rule rejects bad table / column / duplicate name / injection
try:
    db.create_rule(name="bad", condition="latest>70", channels=["telegram"], source_table="scraw_nope")
    check("create_rule rejects missing table", False)
except adb.AlertError:
    check("create_rule rejects missing table", True)
try:
    db.create_rule(name="bad2", condition="latest>70", channels=["telegram"], source_table="scraw_alerts_test", value_column="nope")
    check("create_rule rejects missing column", False)
except adb.AlertError:
    check("create_rule rejects missing column", True)
try:
    db.create_rule(name="onchange", condition="latest>70", channels=["telegram"], source_table="scraw_alerts_test")
    check("create_rule rejects duplicate name", False)
except adb.AlertError:
    check("create_rule rejects duplicate name", True)
try:
    db.create_rule(name="inj", condition="latest>70", channels=["telegram"], source_table="scraw_x; DROP TABLE sources;--")
    check("injection blocked in create_rule", False)
except adb.AlertError:
    check("injection blocked in create_rule", True)
try:
    db.get_series_latest("scraw_x; DROP", {}, "date", "value", 5)
    check("injection blocked in get_series_latest", False)
except adb.AlertError:
    check("injection blocked in get_series_latest", True)

# ── 6. on_change cycle: false→true→false→true ───────────────────
out1 = engine.run_rule("onchange")
check("on_change first eval fires (None→true)", out1.get("fired") is True, str(out1))
n1 = len(db.list_events("onchange"))
check("on_change first fire inserted an event", n1 == 1, f"got {n1}")

out1b = engine.run_rule("onchange")
check("on_change refire while true blocked", out1b.get("fired") is False and "already true" in out1b.get("reason", ""), str(out1b))
check("no extra event while true", len(db.list_events("onchange")) == 1)

# push latest below threshold → condition false → last_state=False, no event
with db.engine.begin() as conn:
    conn.execute(adb.text("INSERT INTO scraw_alerts_test (date, value) VALUES (:d,:v)"), {"d": "d3", "v": 65.0})
out2 = engine.run_rule("onchange")
check("on_change false eval no fire", out2.get("fired") is False and "condition false" in out2.get("reason", ""), str(out2))
check("false eval inserts no event", len(db.list_events("onchange")) == 1)

# push latest back above threshold (prev=65, latest=75) → false→true → fires
with db.engine.begin() as conn:
    conn.execute(adb.text("INSERT INTO scraw_alerts_test (date, value) VALUES (:d,:v)"), {"d": "d4", "v": 75.0})
out3 = engine.run_rule("onchange")
check("on_change false→true fires", out3.get("fired") is True, str(out3))
check("second event inserted", len(db.list_events("onchange")) == 2)

# ── 7. every_match + cooldown + one failing channel ─────────────
db.create_rule(
    name="cd",
    condition="latest > 70",
    channels=["telegram", "slack"],
    source_table="scraw_alerts_test",
    date_column="date",
    value_column="value",
    fire_mode="every_match",
    cooldown_seconds=3600,
    message_template="$rule_name: $latest",
)
out4 = engine.run_rule("cd")
check("every_match fires when true", out4.get("fired") is True, str(out4))
check("dispatch sent to both channels", len(out4.get("channels", [])) == 2, str(out4.get("channels")))
ok_ch = [c for c in out4.get("channels", []) if c.get("ok")]
fail_ch = [c for c in out4.get("channels", []) if not c.get("ok")]
check("telegram ok in dispatch", any(c["channel"] == "telegram" and c.get("ok") for c in out4.get("channels", [])))
check("slack failure recorded, not raised", any(c["channel"] == "slack" and not c.get("ok") for c in out4.get("channels", [])), str(fail_ch))
ev = db.list_events("cd")
check("cd event inserted with channels_results", len(ev) == 1 and len(ev[0]["channels_results"]) == 2, str(ev))

# cooldown blocks immediate refire
out5 = engine.run_rule("cd")
check("cooldown blocks refire", out5.get("fired") is False and "cooldown" in out5.get("reason", ""), str(out5))
check("cooldown blocked new event", len(db.list_events("cd")) == 1)

# ── 8. delete_rule cascades to events ────────────────────────────
ok = db.delete_rule("onchange")
check("delete_rule onchange ok", ok is True)
check("delete_rule cascaded events", len(db.list_events("onchange")) == 0)

# ── 9. list_channels secret-redaction ───────────────────────────
os.environ["ALERTS_TELEGRAM_BOT_TOKEN"] = "leak-canary-token-xyz"
os.environ["ALERTS_TELEGRAM_CHAT_ID"] = "111"
ch2 = list_channels()
blob = json.dumps(ch2)
tg = next((c for c in ch2 if c["name"] == "telegram"), {})
check("telegram configured after setting token", tg.get("configured") is True, str(tg))
check("list_channels does NOT leak secret", "leak-canary-token-xyz" not in blob, blob[:200])
check("list_channels does not return raw values", all("value" not in c for c in ch2), str(ch2))
del os.environ["ALERTS_TELEGRAM_BOT_TOKEN"]
del os.environ["ALERTS_TELEGRAM_CHAT_ID"]

# ── 10. CLI branches exit without stdio ──────────────────────────
cli = subprocess.run(
    [sys.executable, str(Path(__file__).resolve().parent / "server.py"), "--run-rule", "nonexistent"],
    capture_output=True, text=True, env={**os.environ},
)
check("CLI --run-rule exits 1 + JSON error", cli.returncode == 1 and "rule not found" in cli.stdout, f"rc={cli.returncode} out={cli.stdout[:120]}")

cli_all = subprocess.run(
    [sys.executable, str(Path(__file__).resolve().parent / "server.py"), "--run-all"],
    capture_output=True, text=True, env={**os.environ},
)
check("CLI --run-all exits 0 + JSON summary", cli_all.returncode == 0 and "total" in cli_all.stdout, f"rc={cli_all.returncode} out={cli_all.stdout[:120]}")

print()
if _failures:
    print(f"FAILED: {len(_failures)} — {_failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")

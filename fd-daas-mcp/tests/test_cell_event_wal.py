"""cell_event_wal — engine-side same-transaction provenance collection
(wire-provenance-ledger task groups 3.1/3.2, design D3 stage 1).

Covers the four required behaviors:

- hooks: correction_add writes a WAL row with a unique ``evt_`` id and the
  exact payload contract (plus snapshot/dangling/revert/delete events);
- crash semantics: a statement failing AFTER the correction write rolls the
  data change and the WAL row back together — same-transaction collection,
  never "data changed but event lost" nor an orphan event;
- forwarder round-trip: ``provenance_wal_pending`` → ``provenance_wal_ack``
  (pending no longer returns acked rows; re-ack is a 0-count no-op;
  after_seq cursor and the 1000 limit clamp behave);
- old-api compatibility: the pre-WAL tool surface runs error-free on a
  database where the WAL table does not even exist yet, and the WAL reader
  tools degrade gracefully there.

Isolation: each test points ``DAAS_DATABASE_URL`` at its own ``tmp_path``
database. conftest's autouse ``reset_registry`` discards the registry build
cache per test, and every rebuild re-imports the group modules (including
``daas_database``) fresh — so the tools' first ``get_database()`` call binds
to this URL. Raw WAL assertions go through the loaded tools' own module
globals (never a test-file ``daas_database`` import, whose singleton may be
a different module instance than the registry-loaded copy).
"""
from __future__ import annotations

import json
import re
import sqlite3

import pytest

from daas.fd_daas_mcp import registry

COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "name", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
]

ROWS = [
    {"id": 1, "name": "甲", "amount": 10.5},
    {"id": 2, "name": "乙", "amount": 20.0},
    {"id": 3, "name": "丙", "amount": 30.25},
]

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture(autouse=True)
def _fresh_engine_db(tmp_path, monkeypatch):
    """Per-test engine database under tmp_path (see module docstring)."""
    monkeypatch.setenv("DAAS_DATABASE_URL", f"sqlite:///{tmp_path}/wal_engine.db")
    yield


def _t() -> dict:
    return {
        registry.namespaced(g, n): fn
        for g, n, fn in registry.build(profile="cell")
    }


def _seed(key: str, rows: list | None = None, pk: list | None = None) -> dict:
    t = _t()
    begun = t["customer_dataset_ingest_begin"](dataset_key=key, columns=COLS, pk_columns=pk)
    assert "error" not in begun, begun
    chunked = t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"], rows=ROWS if rows is None else rows, start_index=0
    )
    assert "error" not in chunked, chunked
    committed = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" not in committed, committed
    return committed


def _tools_module():
    """The registry-loaded customer_dataset_tools module (via a tool's globals)."""
    return _t()["customer_dataset_preview"].__globals__


def _wal_rows() -> list[tuple]:
    """Raw WAL read through the tools' own database accessor."""
    return _tools_module()["_raw_query_all"](
        "SELECT seq, event_id, type, payload, created_at, acked FROM cell_event_wal"
        " ORDER BY seq ASC"
    )


def _anchor_of(key: str, name: str) -> str:
    got = _t()["customer_dataset_preview"](dataset_key=key)
    assert "error" not in got, got
    for row in got["rows"]:
        if row["name"] == name:
            return row["__row_id"]
    raise AssertionError(f"row {name!r} not found in {key!r}")


# ── hooks: WAL rows for business changes ─────────────────────────────


def test_correction_add_writes_wal_row():
    _seed("wal_a", ROWS)
    anchor = _anchor_of("wal_a", "乙")
    added = _t()["customer_dataset_correction_add"](
        dataset_key="wal_a", op="update", anchor=anchor, values={"amount": 99.5}, actor="alice"
    )
    assert "error" not in added, added

    rows = _wal_rows()
    assert len(rows) == 2  # snapshot.refreshed (seed) + correction.added
    seq2, event_id, type_, payload, created_at, acked = rows[1]
    assert seq2 > rows[0][0]
    assert event_id.startswith("evt_") and len(event_id) == 4 + 32
    assert rows[0][1] != event_id  # event_id unique per row (UNIQUE in DDL too)
    assert type_ == "dataset.correction.added"
    assert json.loads(payload) == {
        "dataset_key": "wal_a",
        "op": "update",
        "anchor": anchor,
        "correction_id": added["correction_id"],
        "actor": "alice",
    }
    assert _ISO_Z.match(created_at), created_at
    assert acked == 0  # fresh events await the forwarder


def test_wal_event_id_uniqueness_enforced_by_ddl():
    _seed("wal_uniq", ROWS)
    first_id = _wal_rows()[0][1]
    tx = _tools_module()["_tx"]
    with tx() as cur:
        with pytest.raises(sqlite3.IntegrityError):
            cur.execute(
                "INSERT INTO cell_event_wal (event_id, type, payload, created_at)"
                " VALUES (?, 'x', '{}', '2026-01-01T00:00:00.000Z')",
                (first_id,),
            )


# ── crash semantics: same-transaction data + event ───────────────────


def test_crash_after_write_rolls_back_data_and_wal_together():
    _seed("crash_a", ROWS)
    tools = _tools_module()
    real_append = tools["_wal_append"]

    def append_then_die(cur, type_, payload):
        real_append(cur, type_, payload)  # WAL row written inside the tx...
        raise RuntimeError("simulated crash after the write")

    tools["_wal_append"] = append_then_die
    try:
        anchor = _anchor_of("crash_a", "甲")
        failed = _t()["customer_dataset_correction_add"](
            dataset_key="crash_a", op="update", anchor=anchor,
            values={"amount": 1.0}, actor="eve",
        )
    finally:
        tools["_wal_append"] = real_append

    assert "error" in failed and "simulated crash" in failed["error"]

    t = _t()
    # the correction row is gone…
    history = t["customer_dataset_correction_list"](dataset_key="crash_a")
    assert history["corrections"] == []
    # …the effective value never changed…
    got = t["customer_dataset_preview"](dataset_key="crash_a")
    by_name = {r["name"]: r for r in got["rows"]}
    assert by_name["甲"]["amount"] == 10.5
    # …and the WAL row that was physically INSERTed died with the transaction
    pending = t["customer_dataset_provenance_wal_pending"]()
    assert [e["type"] for e in pending["events"]] == ["dataset.snapshot.refreshed"]


# ── forwarder round-trip: pending / ack ──────────────────────────────


def test_pending_ack_roundtrip_and_idempotency():
    _seed("rt_a", ROWS)
    t = _t()
    anchor = _anchor_of("rt_a", "甲")
    add1 = t["customer_dataset_correction_add"](
        dataset_key="rt_a", op="update", anchor=anchor, values={"amount": 777.0}, actor="alice"
    )
    assert "error" not in add1, add1

    pending = t["customer_dataset_provenance_wal_pending"]()
    events = pending["events"]
    assert [e["type"] for e in events] == [
        "dataset.snapshot.refreshed",
        "dataset.correction.added",
    ]
    assert events[0]["payload"] == {"dataset_key": "rt_a", "rows": 3}
    assert events[1]["payload"] == {
        "dataset_key": "rt_a", "op": "update", "anchor": anchor,
        "correction_id": add1["correction_id"], "actor": "alice",
    }
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert pending["last_seq"] == seqs[-1]
    assert all(e["event_id"].startswith("evt_") for e in events)

    # a revert mid-flight appends its own event, still un-acked → all visible
    reverted = t["customer_dataset_correction_revert"](
        dataset_key="rt_a", correction_id=add1["correction_id"], actor="bob"
    )
    assert "error" not in reverted, reverted
    pending = t["customer_dataset_provenance_wal_pending"]()
    assert [e["type"] for e in pending["events"]] == [
        "dataset.snapshot.refreshed", "dataset.correction.added", "dataset.correction.reverted",
    ]
    assert pending["events"][2]["payload"] == {
        "dataset_key": "rt_a", "correction_id": add1["correction_id"], "actor": "bob",
    }

    # ack the whole first page
    first = t["customer_dataset_provenance_wal_ack"](upto_seq=pending["last_seq"])
    assert first == {"acked": 3}
    # re-ack is a no-op (idempotent — event_id-level dedup sits above this)
    again = t["customer_dataset_provenance_wal_ack"](upto_seq=pending["last_seq"])
    assert again == {"acked": 0}
    # acked rows leave the pending set
    after_ack = t["customer_dataset_provenance_wal_pending"]()
    assert after_ack["events"] == []
    # …but the rows are still physically there (only the acked bit moved)
    assert len(_wal_rows()) == 3 and all(r[5] == 1 for r in _wal_rows())

    # a new event after the ack shows up alone
    add2 = t["customer_dataset_correction_add"](
        dataset_key="rt_a", op="update", anchor=anchor, values={"amount": 888.0}, actor="carol"
    )
    assert "error" not in add2, add2
    pending2 = t["customer_dataset_provenance_wal_pending"]()
    assert [e["type"] for e in pending2["events"]] == ["dataset.correction.added"]
    assert pending2["events"][0]["seq"] > pending["last_seq"]


def test_pending_after_seq_cursor_and_limit_clamp():
    _seed("cur_a", ROWS)
    t = _t()
    anchor = _anchor_of("cur_a", "甲")
    for value in (1.0, 2.0, 3.0):
        added = t["customer_dataset_correction_add"](
            dataset_key="cur_a", op="update", anchor=anchor, values={"amount": value}
        )
        assert "error" not in added, added
    # 1 snapshot + 3 corrections = 4 events

    p1 = t["customer_dataset_provenance_wal_pending"](limit=2)
    assert [e["seq"] for e in p1["events"]] == [1, 2]
    p2 = t["customer_dataset_provenance_wal_pending"](after_seq=p1["last_seq"], limit=2)
    assert [e["seq"] for e in p2["events"]] == [3, 4]
    p3 = t["customer_dataset_provenance_wal_pending"](after_seq=p2["last_seq"])
    assert p3["events"] == [] and p3["last_seq"] == p2["last_seq"]

    # limit clamps: giant → capped at 1000 (all 4 returned), zero/negative → 1
    big = t["customer_dataset_provenance_wal_pending"](limit=999_999)
    assert len(big["events"]) == 4
    one = t["customer_dataset_provenance_wal_pending"](limit=0)
    assert len(one["events"]) == 1
    neg = t["customer_dataset_provenance_wal_pending"](limit=-5)
    assert len(neg["events"]) == 1

    # ack honors the cursor even for stale (already-forwarded) seqs
    assert t["customer_dataset_provenance_wal_ack"](upto_seq=0) == {"acked": 0}


# ── dangling + delete events ─────────────────────────────────────────


def test_dangling_and_delete_events():
    _seed("dg_a", ROWS)
    t = _t()
    anchor = _anchor_of("dg_a", "丙")
    added = t["customer_dataset_correction_add"](
        dataset_key="dg_a", op="update", anchor=anchor, values={"amount": 1.0}
    )
    assert "error" not in added, added
    # re-sync without 丙 → replay marks the correction dangling
    _seed("dg_a", ROWS[:2])
    dangling = t["customer_dataset_correction_dangling"](dataset_key="dg_a")
    assert len(dangling["corrections"]) == 1

    pending = t["customer_dataset_provenance_wal_pending"]()
    assert [e["type"] for e in pending["events"]] == [
        "dataset.snapshot.refreshed",      # first commit
        "dataset.correction.added",
        "dataset.snapshot.refreshed",      # re-sync commit
        "dataset.correction.dangling",     # marked inside the re-sync tx
    ]
    assert pending["events"][3]["payload"] == {
        "dataset_key": "dg_a",
        "correction_id": added["correction_id"],
        "reason": "行锚在最新快照中无匹配行",
    }

    # dataset deletion emits its event in the delete transaction — and the
    # WAL row OUTLIVES the dataset's own metadata rows
    deleted = t["customer_dataset_delete"](dataset_key="dg_a", actor="dave")
    assert deleted == {"deleted": True}
    pending = t["customer_dataset_provenance_wal_pending"]()
    assert pending["events"][-1]["type"] == "dataset.deleted"
    assert pending["events"][-1]["payload"] == {"dataset_key": "dg_a", "actor": "dave"}
    assert "error" in t["customer_dataset_overview"](dataset_key="dg_a")


# ── old-api compatibility ────────────────────────────────────────────


def test_pre_wal_database_and_legacy_surface_smoke():
    t = _t()
    # WAL reader tools on a database where the WAL table does not exist yet:
    # graceful empty page / zero ack, never "no such table"
    assert t["customer_dataset_provenance_wal_pending"]() == {"events": [], "last_seq": 0}
    assert t["customer_dataset_provenance_wal_ack"](upto_seq=99) == {"acked": 0}

    # the pre-WAL flow, never touching the WAL tools: zero errors end to end
    _seed("legacy_a", ROWS, pk=["id"])
    overview = t["customer_dataset_overview"](dataset_key="legacy_a")
    assert overview["rows"] == 3 and "error" not in overview
    preview = t["customer_dataset_preview"](dataset_key="legacy_a", include_corrected=False)
    assert preview["total"] == 3 and "error" not in preview
    added = t["customer_dataset_correction_add"](
        dataset_key="legacy_a", op="insert", values={"id": 9, "name": "新", "amount": 5.0},
        actor="legacy",
    )
    assert "error" not in added, added
    listing = t["customer_dataset_correction_list"](dataset_key="legacy_a")
    assert "error" not in listing and len(listing["corrections"]) == 1
    reverted = t["customer_dataset_correction_revert"](
        dataset_key="legacy_a", correction_id=added["correction_id"]
    )
    assert reverted == {"reverted": True}
    aborted_flow = t["customer_dataset_ingest_begin"](dataset_key="legacy_a", columns=COLS)
    assert "error" not in aborted_flow, aborted_flow
    assert t["customer_dataset_ingest_abort"](ingest_id=aborted_flow["ingest_id"]) == {
        "aborted": True
    }
    assert t["customer_dataset_delete"](dataset_key="legacy_a") == {"deleted": True}

    # and the two new tools surface under the namespaced wire-facing name
    names = {n for _, n, _ in registry.build(profile="cell")}
    assert {"provenance_wal_pending", "provenance_wal_ack"} <= names

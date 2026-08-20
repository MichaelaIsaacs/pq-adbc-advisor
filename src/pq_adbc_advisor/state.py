"""Persistent per-workspace telemetry state.

Purpose: give the PM team the ability to see *improvement over time* by
locking in the first-run risk counts and comparing them against every
subsequent run. Rather than doing longitudinal joins in KQL, every
telemetry event carries both the ``first_run_*`` snapshot and the
``current_run_*`` counters so a single Kusto query can render the delta.

Where state lives:

  1. Preferred: ``/lakehouse/default/Files/pq_adbc_advisor_state.json``
     in the workspace's default lakehouse. This persists across notebook
     sessions and survives kernel restarts.
  2. Fallback: in-process memory. When there is no default lakehouse
     attached, we still emit telemetry but every run reports
     ``run_number=1`` and ``first_run=True``.

State schema::

    {
        "schema_version": 1,
        "first_run_at": "2026-08-18T19:41:00Z",
        "first_run_id": "d5f2...",
        "first_risk_high": 3,
        "first_risk_medium": 5,
        "first_risk_low": 12,
        "first_risk_unknown": 0,
        "first_risk_na": 14,
        "first_custom_dsn": 2,
        "first_total_calls": 34,
        "first_migrating_artifacts": 4,
        "first_pinned_odbc": 3,
        "first_counts_by_connector": {"Snowflake": 4, "Amazon Redshift": 2, ...},
        "run_count": 7,
        "last_run_at": "2026-08-25T08:12:03Z"
    }
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

STATE_SCHEMA_VERSION = 1
_LAKEHOUSE_PATH = "/lakehouse/default/Files/pq_adbc_advisor_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_state() -> dict[str, Any] | None:
    """Return persisted state, or None if unavailable.

    We try the default-lakehouse path first. If it doesn't exist or can't
    be read, we return None and the caller treats the run as a first run.
    """
    try:
        if os.path.exists(_LAKEHOUSE_PATH):
            with open(_LAKEHOUSE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("schema_version") == STATE_SCHEMA_VERSION:
                return data
    except Exception:
        pass
    return None


def is_telemetry_opted_out() -> bool:
    """Return True when the customer has persistently opted out of telemetry."""
    state = load_state()
    return bool(state and state.get("telemetry_opt_out"))


def set_telemetry_opt_out(opt_out: bool) -> bool:
    """Persist an opt-out flag alongside first-run baseline state.

    Returns True on successful write to the lakehouse.
    """
    existing = load_state() or {
        "schema_version": STATE_SCHEMA_VERSION,
        "first_run_at": _now_iso(),
    }
    if opt_out:
        existing["telemetry_opt_out"] = True
        existing["telemetry_opt_out_at"] = _now_iso()
    else:
        existing.pop("telemetry_opt_out", None)
        existing.pop("telemetry_opt_out_at", None)
    return save_state(existing)


def save_state(state: dict[str, Any]) -> bool:
    """Persist state to the default lakehouse. Return True on success.

    v0.2.4: adds a POSIX advisory lock so concurrent scans (two notebooks
    against the same workspace) do not race and clobber each other's
    first-run snapshot. Windows has no fcntl; we rely on the atomic
    rename instead.
    """
    tmp_path = _LAKEHOUSE_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_LAKEHOUSE_PATH), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            try:
                import fcntl  # Unix only
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                # fcntl unavailable (Windows) or not supported on this FS.
                # Atomic rename below is still race-safe on POSIX.
                pass
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _LAKEHOUSE_PATH)
        return True
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


def compute_run_state(current_counts: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Read existing state, compute this run's contribution, persist it.

    Args:
        current_counts: The output of ImpactReport._counts() plus a few
            extras (see telemetry.py for the fields we look for).
        run_id: A UUID for this specific run.

    Returns a dict merging the persisted "first_*" fields with the
    supplied current-run fields, ready to be included in a telemetry
    event.
    """
    existing = load_state()
    now = _now_iso()

    if existing is None:
        # First run for this workspace (or state is missing / unreadable).
        new_state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "first_run_at": now,
            "first_run_id": run_id,
            "first_risk_high": current_counts.get("risk_high", 0),
            "first_risk_medium": current_counts.get("risk_medium", 0),
            "first_risk_low": current_counts.get("risk_low", 0),
            "first_risk_unknown": current_counts.get("risk_unknown", 0),
            "first_risk_na": current_counts.get("risk_na", 0),
            "first_custom_dsn": current_counts.get("custom_dsn", 0),
            "first_total_calls": current_counts.get("total_calls", 0),
            "first_migrating_artifacts": current_counts.get("migrating_artifacts", 0),
            "first_pinned_odbc": current_counts.get("pinned_odbc", 0),
            "first_counts_by_connector": current_counts.get("counts_by_connector", {}),
            "run_count": 1,
            "last_run_at": now,
        }
        save_state(new_state)
        return {
            **{k: new_state[k] for k in new_state if k.startswith("first_") or k in ("run_count",)},
            "is_first_run": True,
        }

    # Subsequent run: preserve first_* snapshot, bump the run counter.
    new_state = dict(existing)
    new_state["run_count"] = int(existing.get("run_count", 1)) + 1
    new_state["last_run_at"] = now
    save_state(new_state)
    return {
        **{k: existing[k] for k in existing if k.startswith("first_") or k == "run_count"},
        "run_count": new_state["run_count"],
        "is_first_run": False,
    }


def reset_state() -> bool:
    """Delete the persisted state file so the next scan is a "first run" again."""
    try:
        if os.path.exists(_LAKEHOUSE_PATH):
            os.remove(_LAKEHOUSE_PATH)
        return True
    except Exception:
        return False

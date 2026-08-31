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


def _home_fallback_path() -> str:
    """Local fallback: ``~/.pq-adbc-advisor/state.json``.

    v0.3.4: when running outside Fabric (local dev, CI, ADO agent) the
    lakehouse path doesn't exist, so save_state used to silently fail and
    every scan looked like ``is_first_run=True``. This fallback keeps the
    first-run baseline intact so the "improvement over time" telemetry
    actually shows improvement.
    """
    return os.path.expanduser("~/.pq-adbc-advisor/state.json")


def _state_paths() -> list[str]:
    """Return the ordered list of paths to try (lakehouse, then home)."""
    return [_LAKEHOUSE_PATH, _home_fallback_path()]


def state_backend() -> str:
    """Return which backend the next write / read will use.

    Values: ``"lakehouse"`` (Fabric default lakehouse writable),
    ``"home"`` (local fallback under ~/.pq-adbc-advisor), or
    ``"memory"`` (nothing writable — telemetry deltas will not persist).

    Cheap enough to call from telemetry.emit_scan_summary so we can send
    the backend as a customDimension and prove baselines are persisting.
    """
    for path in _state_paths():
        try:
            parent = os.path.dirname(path)
            if not parent:
                continue
            os.makedirs(parent, exist_ok=True)
            probe = path + ".probe"
            with open(probe, "w") as f:
                f.write("")
            os.remove(probe)
            return "lakehouse" if path == _LAKEHOUSE_PATH else "home"
        except Exception:
            continue
    return "memory"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_state() -> dict[str, Any] | None:
    """Return persisted state, or None if unavailable.

    Reads from the lakehouse path first; falls back to ~/.pq-adbc-advisor
    (v0.3.4) so local dev runs preserve first-run baselines too.
    """
    for path in _state_paths():
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("schema_version") == STATE_SCHEMA_VERSION:
                    return data
        except Exception:
            continue
    return None


def is_telemetry_opted_out() -> bool:
    """Return True when the customer has persistently opted out of telemetry."""
    state = load_state()
    return bool(state and state.get("telemetry_opt_out"))


def set_telemetry_opt_out(opt_out: bool) -> bool:
    """Persist an opt-out flag alongside first-run baseline state.

    Returns True on successful write.
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
    """Persist state to the first writable backend. Return True on success.

    v0.2.4: adds a POSIX advisory lock so concurrent scans don't race.
    v0.3.4: on lakehouse failure, silently falls back to
    ``~/.pq-adbc-advisor/state.json`` and prints a one-time warning so
    users understand telemetry baselines are still being preserved (just
    outside the lakehouse). If BOTH backends fail, prints a clearer
    warning and returns False — the caller degrades to in-memory only,
    so subsequent scans in the same kernel keep incrementing run_count
    but a kernel restart resets to first_run=True.
    """
    for path in _state_paths():
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                try:
                    import fcntl  # Unix only
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            if path != _LAKEHOUSE_PATH:
                _warn_once_home_fallback(path)
            return True
        except Exception:
            # Try the next backend.
            try:
                os.remove(path + ".tmp")
            except OSError:
                pass
            continue

    _warn_once_no_backend()
    return False


def _warn_once_home_fallback(path: str) -> None:
    if getattr(_warn_once_home_fallback, "_printed", False):
        return
    _warn_once_home_fallback._printed = True  # type: ignore[attr-defined]
    print(
        f"[pq-adbc-advisor] No default lakehouse detected — persisting "
        f"telemetry baseline to {path}. This keeps 'improvement over "
        f"time' metrics working on local / CI runs."
    )


def _warn_once_no_backend() -> None:
    if getattr(_warn_once_no_backend, "_printed", False):
        return
    _warn_once_no_backend._printed = True  # type: ignore[attr-defined]
    print(
        "[pq-adbc-advisor] Could not persist state to /lakehouse/ or "
        "~/.pq-adbc-advisor/. Telemetry will report is_first_run=True on "
        "every scan until a writable location is available."
    )


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
    """Delete persisted state from every backend so the next scan is a
    "first run" again. Returns True when at least one file was removed
    or nothing was found to remove (both mean "state is clean now").
    """
    ok = True
    for path in _state_paths():
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            ok = False
    return ok

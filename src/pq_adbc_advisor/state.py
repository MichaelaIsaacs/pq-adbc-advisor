"""Persistent per-workspace telemetry state.

Purpose: give the PM team the ability to see *improvement over time* by
locking in the first-run risk counts and comparing them against every
subsequent run. Rather than doing longitudinal joins in KQL, every
telemetry event carries both the ``first_run_*`` snapshot and the
``current_run_*`` counters so a single Kusto query can render the delta.

Storage layout (v0.3.5, rewritten during the deep bug bash):

Two independent kinds of state, kept in SEPARATE files so lifecycle
events on one never corrupt the other:

  1. Baseline state — workspace-scoped, contains the ``first_*``
     counters and ``run_count``.
       * Fabric: ``/lakehouse/default/Files/pq_adbc_advisor_state.json``
         (already workspace-scoped because /lakehouse is mounted from
         the workspace's default lakehouse).
       * Local / CI: ``~/.pq-adbc-advisor/state-{tenant}-{workspace}.json``
         so scanning workspace A does not overwrite workspace B's
         baseline when both run from the same laptop.
  2. Opt-out flag — machine-wide, tiny sidecar file. ``disable_telemetry()``
     writes it once; every future emission across every workspace reads
     it. Never touches baseline state.

Concurrency (v0.3.5):

  * ``compute_run_state`` acquires an exclusive lock on a sidecar
    ``.lock`` file that covers the whole read-modify-write cycle.
    Prevents lost-update races between concurrent scans in the same
    workspace.
  * ``save_state`` writes to a per-writer unique tmp file
    (``{path}.tmp.{pid}.{uuid}``). Prevents the double-open truncate
    corruption where two writers to the same shared tmp would clobber
    each other and split state across backends.
  * Schema-version guard on WRITE: refuses to overwrite a state file
    with a HIGHER schema_version than the writer's own (protects a
    user who accidentally downgrades from wiping their v2 baseline).

Baseline schema::

    {
        "schema_version": 1,
        "workspace_id": "…",         # v0.3.5 — for validation on read
        "tenant_hash": "…",          # v0.3.5 — for validation on read
        "first_run_at": "…",
        "first_run_id": "…",
        "first_risk_high": 3,
        "first_risk_medium": 5,
        "first_risk_low": 12,
        "first_risk_unknown": 0,
        "first_risk_na": 14,
        "first_custom_dsn": 2,
        "first_total_calls": 34,
        "first_migrating_artifacts": 4,
        "first_pinned_odbc": 3,
        "first_counts_by_connector": {"Snowflake": 4, ...},
        "run_count": 7,
        "last_run_at": "…"
    }
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

STATE_SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

_LAKEHOUSE_PATH = "/lakehouse/default/Files/pq_adbc_advisor_state.json"
_HOME_DIR = "~/.pq-adbc-advisor"

# Machine-wide opt-out flag lives in its own file. Never touched by
# baseline mutations, so disable→enable→disable never corrupts a baseline.
_LAKEHOUSE_OPT_OUT = "/lakehouse/default/Files/pq_adbc_advisor_opt_out.json"

# v0.3.8 SECURITY: telemetry buffer + persistent send log are HOME-ONLY.
# Prior versions (v0.3.7) wrote these to /lakehouse/default/Files/, which
# is shared across every user of the workspace. Buffered envelopes carry
# workspace_id + tenant_hash + user_hash, so a cross-workspace lakehouse
# reader could see other users' telemetry metadata (Natasha finding
# HIGH-1). Fabric notebook home directories are per-notebook-kernel and
# isolated per user session, so writing here is naturally private.
#
# Trade-off: the buffer no longer survives a kernel restart. That is
# acceptable because the buffer's purpose is within-session retries
# against transient proxy/5xx failures. Cross-session persistence of
# individual failed events was never worth the cross-user disclosure.
_HOME_TELEMETRY_BUFFER = "telemetry_buffer.json"
_HOME_TELEMETRY_LOG = "telemetry_log.json"
_TELEMETRY_LOG_CAP = 200
_TELEMETRY_BUFFER_CAP = 500  # hard limit so a persistently-offline environment cannot balloon disk


def _sanitize(component: str) -> str:
    """Make a string safe for a filename (Bug 2 fix)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", component or "unknown")[:64]


def _home_dir() -> str:
    return os.path.expanduser(_HOME_DIR)


def _home_baseline_path(workspace_id: str | None, tenant_hash: str | None) -> str:
    """Bug 2 fix: home-fallback baseline path is keyed by workspace + tenant.

    Scanning multiple workspaces from the same machine no longer causes
    the first workspace's baseline to shadow every subsequent scan.
    Legacy callers that don't supply a workspace_id fall back to
    ``state-legacy.json`` — same as pre-v0.3.5 behavior — so tests and
    tools that never learned the new signature still work.
    """
    ws = _sanitize(workspace_id) if workspace_id else "legacy"
    th = _sanitize(tenant_hash) if tenant_hash else "notenant"
    return os.path.join(_home_dir(), f"state-{th}-{ws}.json")


def _home_opt_out_path() -> str:
    return os.path.join(_home_dir(), "opt_out.json")


def _baseline_paths(workspace_id: str | None, tenant_hash: str | None) -> list[str]:
    """Return the ordered list of baseline paths (lakehouse first)."""
    return [_LAKEHOUSE_PATH, _home_baseline_path(workspace_id, tenant_hash)]


def _opt_out_paths() -> list[str]:
    return [_LAKEHOUSE_OPT_OUT, _home_opt_out_path()]


# --------------------------------------------------------------------------- #
# Backend probe
# --------------------------------------------------------------------------- #

def state_backend(workspace_id: str | None = None, tenant_hash: str | None = None) -> str:
    """Return which backend the next baseline write will use.

    Values: ``"lakehouse"`` (Fabric default lakehouse writable),
    ``"home"`` (local fallback under ~/.pq-adbc-advisor), or
    ``"memory"`` (nothing writable — telemetry deltas will not persist).
    """
    for path in _baseline_paths(workspace_id, tenant_hash):
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


# --------------------------------------------------------------------------- #
# Time helper
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Cross-process + in-process lock
# --------------------------------------------------------------------------- #

# Bug 4 fix: in-process guard for asyncio / thread-pool concurrency in the
# same kernel. Different from the file lock (which is cross-process).
_INPROC_LOCK = threading.Lock()


@contextlib.contextmanager
def _locked(path: str) -> Iterator[None]:
    """Hold an exclusive lock spanning the whole read-modify-write cycle.

    Uses a sidecar ``{path}.lock`` file — a stable, dedicated inode we
    never rename, so ``os.replace`` on the state file can't yank the
    lock out from under a concurrent writer (which was the pre-v0.3.5
    Bug 5 failure mode).
    """
    lock_path = path + ".lock"
    parent = os.path.dirname(lock_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception:
            pass
    fd = None
    try:
        with _INPROC_LOCK:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    import fcntl  # Unix only
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
            except OSError:
                fd = None
            try:
                yield
            finally:
                if fd is not None:
                    try:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass
    finally:
        pass


# --------------------------------------------------------------------------- #
# Low-level JSON read/write with schema-version guard (Bug 6)
# --------------------------------------------------------------------------- #

def _read_json(path: str) -> dict[str, Any] | None:
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _write_json_atomic(path: str, payload: dict[str, Any]) -> bool:
    """Atomic write with a UNIQUE tmp path per writer (Bug 5 fix).

    * Different tmp paths per writer → no truncate/rename collision.
    * Schema-version guard (Bug 6): refuses to overwrite a target whose
      persisted schema_version is HIGHER than the writer's — prevents a
      downgraded client from wiping a newer client's baseline.
    """
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception:
            return False

    # Bug 6: refuse to clobber a newer schema.
    existing = _read_json(path)
    if existing is not None:
        existing_ver = existing.get("schema_version")
        writer_ver = payload.get("schema_version", STATE_SCHEMA_VERSION)
        try:
            if isinstance(existing_ver, int) and existing_ver > int(writer_ver):
                _warn_once_schema_downgrade(path, existing_ver, int(writer_ver))
                return False
        except (TypeError, ValueError):
            pass

    tmp_path = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
        return True
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


# --------------------------------------------------------------------------- #
# Baseline load / save (public API)
# --------------------------------------------------------------------------- #

def load_state(
    workspace_id: str | None = None,
    tenant_hash: str | None = None,
) -> dict[str, Any] | None:
    """Return the persisted baseline for a workspace, or None.

    v0.3.5:
      * Home fallback path is keyed by tenant + workspace (Bug 2).
      * Cross-check on read: if the on-disk baseline records a different
        workspace_id or tenant_hash than the caller supplied, we treat
        it as "no baseline" so we don't cross-contaminate.
    """
    for path in _baseline_paths(workspace_id, tenant_hash):
        data = _read_json(path)
        if data is None:
            continue
        if data.get("schema_version") != STATE_SCHEMA_VERSION:
            continue
        # Belt-and-suspenders identity check.
        if workspace_id and data.get("workspace_id") and data["workspace_id"] != workspace_id:
            continue
        if tenant_hash and data.get("tenant_hash") and data["tenant_hash"] != tenant_hash:
            continue
        return data
    return None


def save_state(
    state: dict[str, Any],
    workspace_id: str | None = None,
    tenant_hash: str | None = None,
) -> bool:
    """Persist baseline state to the first writable backend.

    Public signature stays back-compat: legacy callers that pass only
    ``state`` still work, using the legacy home path. New callers should
    pass workspace_id + tenant_hash to get per-workspace persistence.
    """
    ws = workspace_id or state.get("workspace_id")
    th = tenant_hash or state.get("tenant_hash")
    for path in _baseline_paths(ws, th):
        if _write_json_atomic(path, state):
            if path != _LAKEHOUSE_PATH:
                _warn_once_home_fallback(path)
            return True
    _warn_once_no_backend()
    return False


# --------------------------------------------------------------------------- #
# Opt-out (separate file — Bug 1 + Bug 3 fix)
# --------------------------------------------------------------------------- #

def is_telemetry_opted_out() -> bool:
    """True when ANY opt-out backend has the flag set (Bug 3 fix).

    Reading only the highest-priority backend used to let a lakehouse
    baseline silently override a home-fallback opt-out. We now consult
    every backend and honor the STRONGEST signal (opt-out wins).
    """
    for path in _opt_out_paths():
        data = _read_json(path)
        if data and data.get("telemetry_opt_out"):
            return True
    return False


def set_telemetry_opt_out(opt_out: bool) -> bool:
    """Persist / clear the machine-wide opt-out flag.

    Bug 1 fix: opt-out lives in its own file. Baseline state is
    completely untouched, so ``disable_telemetry() → enable_telemetry()
    → first real scan`` still emits ``is_first_run=True`` with a real
    baseline and still prints the first-run privacy notice.
    """
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "telemetry_opt_out": bool(opt_out),
        "updated_at": _now_iso(),
    }
    wrote_any = False
    for path in _opt_out_paths():
        if opt_out:
            if _write_json_atomic(path, payload):
                wrote_any = True
        else:
            # Clearing: remove every opt-out file so the "OR across backends"
            # read semantics don't leave an old flag sticky.
            try:
                if os.path.exists(path):
                    os.remove(path)
                wrote_any = True
            except Exception:
                continue
    return wrote_any


# --------------------------------------------------------------------------- #
# compute_run_state (public API, called by telemetry.emit_scan_summary)
# --------------------------------------------------------------------------- #

def compute_run_state(
    current_counts: dict[str, Any],
    run_id: str,
    workspace_id: str | None = None,
    tenant_hash: str | None = None,
) -> dict[str, Any]:
    """Read existing state, compute this run's contribution, persist it.

    v0.3.5:
      * Whole read-modify-write cycle is locked (Bug 4 fix) using a
        sidecar ``.lock`` file plus an in-process ``threading.Lock``.
      * ``existing`` is only treated as a real baseline if it has the
        expected ``first_risk_high`` key (Bug 1 fix). A stub file with
        only ``schema_version`` + ``first_run_at`` is upgraded to a
        real baseline on this scan and reports ``is_first_run=True``.
    """
    now = _now_iso()
    primary = _baseline_paths(workspace_id, tenant_hash)[0]

    with _locked(primary):
        existing = load_state(workspace_id, tenant_hash)

        # Bug 1: a state file with no baseline snapshot is not really a
        # subsequent run. Treat it as first-run so the baseline is
        # actually locked in and the privacy notice fires.
        has_real_baseline = bool(existing and "first_risk_high" in existing)

        if not has_real_baseline:
            new_state = {
                "schema_version": STATE_SCHEMA_VERSION,
                "workspace_id": workspace_id or "",
                "tenant_hash": tenant_hash or "",
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
            save_state(new_state, workspace_id, tenant_hash)
            return {
                **{k: new_state[k] for k in new_state if k.startswith("first_") or k == "run_count"},
                "is_first_run": True,
            }

        # Subsequent run: preserve first_* snapshot, bump the run counter.
        new_state = dict(existing)
        new_state["run_count"] = int(existing.get("run_count", 1)) + 1
        new_state["last_run_at"] = now
        save_state(new_state, workspace_id, tenant_hash)
        return {
            **{k: existing[k] for k in existing if k.startswith("first_") or k == "run_count"},
            "run_count": new_state["run_count"],
            "is_first_run": False,
        }


# --------------------------------------------------------------------------- #
# reset_state
# --------------------------------------------------------------------------- #

def reset_state(
    workspace_id: str | None = None,
    tenant_hash: str | None = None,
) -> bool:
    """Delete persisted baseline state from every backend so the next
    scan is a "first run" again. Also removes the sidecar lock file.
    Opt-out flag is NOT touched.
    """
    ok = True
    paths = _baseline_paths(workspace_id, tenant_hash)
    # Also try the legacy pre-v0.3.5 single-file home fallback so old
    # installs don't leave dangling state that shadows the new per-
    # workspace files.
    legacy_home = os.path.join(_home_dir(), "state.json")
    for path in paths + [legacy_home]:
        try:
            if os.path.exists(path):
                os.remove(path)
            lock = path + ".lock"
            if os.path.exists(lock):
                os.remove(lock)
        except Exception:
            ok = False
    return ok


# --------------------------------------------------------------------------- #
# One-time warnings
# --------------------------------------------------------------------------- #

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


def _warn_once_schema_downgrade(path: str, existing: int, writer: int) -> None:
    if getattr(_warn_once_schema_downgrade, "_printed", False):
        return
    _warn_once_schema_downgrade._printed = True  # type: ignore[attr-defined]
    print(
        f"[pq-adbc-advisor] Refusing to overwrite state at {path} "
        f"(persisted schema_version={existing}, this client only "
        f"understands schema_version={writer}). Upgrade the tool to "
        f"regain persistence."
    )


# --------------------------------------------------------------------------- #
# Legacy back-compat shim
# --------------------------------------------------------------------------- #
# Old callers (and pre-v0.3.5 tests) referenced ``_state_paths()`` and
# ``_home_fallback_path()`` — keep them as thin aliases so we don't break
# anything that patched them.

def _state_paths() -> list[str]:
    """Deprecated: returns paths for a legacy (no workspace_id) call."""
    return _baseline_paths(None, None)


def _home_fallback_path() -> str:  # noqa: N802 — kept for monkeypatch compat
    """Deprecated: returns the legacy home path used before v0.3.5."""
    return _home_baseline_path(None, None)


# --------------------------------------------------------------------------- #
# v0.3.8 SECURITY (was v0.3.7) — telemetry buffer + persistent send log
# --------------------------------------------------------------------------- #
#
# **HOME-ONLY, NEVER LAKEHOUSE.** Prior versions (v0.3.7) wrote these files
# to /lakehouse/default/Files/ alongside baseline state. That is a shared
# workspace surface — every user of the workspace can read Files/. Buffered
# envelopes carry workspace_id + tenant_hash + user_hash, so a lakehouse
# reader could inspect other users' telemetry metadata. Natasha's Sept 2026
# review flagged this as HIGH-1 severity.
#
# In v0.3.8 the buffer and log live ONLY in the notebook kernel's home
# directory (~/.pq-adbc-advisor/). Fabric notebook home dirs are per-
# notebook-kernel and isolated per user session, so there is no cross-user
# read surface. Trade-off: buffer no longer survives kernel restart.
# Acceptable because the buffer exists for within-session transient
# retries; cross-session persistence was never worth cross-user leak.
#
# Baseline state and the opt-out flag remain in lakehouse Files/ because
# they only carry the writer's own workspace/tenant hash + first-run counts
# (no other users' data), and they need cross-session persistence for the
# "improvement over time" metric and the opt-out promise.

def _home_telemetry_buffer_path() -> str:
    return os.path.join(_home_dir(), _HOME_TELEMETRY_BUFFER)


def _home_telemetry_log_path() -> str:
    return os.path.join(_home_dir(), _HOME_TELEMETRY_LOG)


def _telemetry_buffer_paths() -> list[str]:
    """v0.3.8 SECURITY: home-only, no lakehouse."""
    return [_home_telemetry_buffer_path()]


def _telemetry_log_paths() -> list[str]:
    """v0.3.8 SECURITY: home-only, no lakehouse."""
    return [_home_telemetry_log_path()]


def _read_first_available(paths: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Return (payload, path) for the first path we can read, or (None, None)."""
    for path in paths:
        try:
            existing = _read_json(path)
            if existing is not None:
                return existing, path
        except Exception:
            continue
    return None, None


def append_telemetry_log(entry: dict[str, Any]) -> None:
    """Append one send-attempt outcome to the persistent log.

    The log is a ring buffer of the last _TELEMETRY_LOG_CAP entries. Never
    raises — the log is best-effort observability, not a critical path.
    Diagnostic history only, not audit evidence (workspace users cannot
    tamper because it's home-only in v0.3.8, but home containers are
    ephemeral in Fabric so cross-session tamper evidence is not preserved).
    """
    paths = _telemetry_log_paths()
    for path in paths:
        try:
            existing, _ = _read_first_available([path])
            log: list[dict[str, Any]] = list((existing or {}).get("entries", []))
            log.append(entry)
            if len(log) > _TELEMETRY_LOG_CAP:
                log = log[-_TELEMETRY_LOG_CAP:]
            payload = {"schema_version": 1, "entries": log}
            if _write_json_atomic(path, payload):
                return
        except Exception:
            continue


def read_telemetry_log() -> list[dict[str, Any]]:
    """Return the persistent send-attempt log, newest last. Empty on error."""
    existing, _ = _read_first_available(_telemetry_log_paths())
    if not existing:
        return []
    entries = existing.get("entries")
    return list(entries) if isinstance(entries, list) else []


def buffer_failed_envelope(item: dict[str, Any]) -> None:
    """Persist a failed AI envelope for a later retry (home-only, per-user).

    Assigns a stable id so `remove_buffered_envelopes` can dedupe. Silently
    drops the oldest entries when _TELEMETRY_BUFFER_CAP is exceeded. Home-
    only in v0.3.8 to prevent cross-user disclosure in shared workspaces.
    """
    item = dict(item)
    item.setdefault("id", str(uuid.uuid4()))
    for path in _telemetry_buffer_paths():
        try:
            existing, _ = _read_first_available([path])
            queue: list[dict[str, Any]] = list((existing or {}).get("envelopes", []))
            queue.append(item)
            if len(queue) > _TELEMETRY_BUFFER_CAP:
                queue = queue[-_TELEMETRY_BUFFER_CAP:]
            payload = {"schema_version": 1, "envelopes": queue}
            if _write_json_atomic(path, payload):
                return
        except Exception:
            continue


def read_buffered_envelopes() -> list[dict[str, Any]]:
    """Return buffered envelopes waiting to be flushed. Empty on error."""
    existing, _ = _read_first_available(_telemetry_buffer_paths())
    if not existing:
        return []
    env = existing.get("envelopes")
    return list(env) if isinstance(env, list) else []


def remove_buffered_envelopes(ids_to_drop: list[str]) -> int:
    """Drop the given ids from the buffer. Returns count removed. Never raises."""
    if not ids_to_drop:
        return 0
    drop_set = {i for i in ids_to_drop if i}
    removed = 0
    for path in _telemetry_buffer_paths():
        try:
            existing, _ = _read_first_available([path])
            if not existing:
                continue
            queue = list(existing.get("envelopes", []))
            kept = [e for e in queue if e.get("id") not in drop_set]
            removed_here = len(queue) - len(kept)
            if removed_here == 0:
                continue
            payload = {"schema_version": 1, "envelopes": kept}
            if _write_json_atomic(path, payload):
                removed = removed_here
                return removed
        except Exception:
            continue
    return removed


# --------------------------------------------------------------------------- #
# v0.3.8 SECURITY — opt-out durability + hard opt-out (Natasha HIGH-2)
# --------------------------------------------------------------------------- #
#
# Prior versions trusted the JSON write to `/lakehouse/default/Files/` to
# be durable. Fabric notebooks have a known footgun: writes to
# /lakehouse/default/Files/ silently redirect to /tmp/ if no default
# lakehouse is attached, so an apparent "success" can be ephemeral. On the
# next kernel restart, the opt-out is lost and telemetry resumes.
#
# v0.3.8 mitigations:
#   1. An in-process HARD opt-out flag that always wins. Once
#      disable_telemetry() is called this session, telemetry is off
#      until the process exits, regardless of file state.
#   2. Environment variable `PQ_ADBC_ADVISOR_TELEMETRY=off|0|false`
#      always defeats telemetry without needing any file to exist.
#   3. Write-through verification: after disable_telemetry() writes
#      the opt-out file, the code reads it back through the same code
#      path and verifies. If verification fails, the API returns False
#      and prints a blocking warning so the customer knows the file
#      persistence is not proven.

_HARD_OPT_OUT = False


def hard_opt_out_active() -> bool:
    """Session-scoped hard opt-out. Set by disable_telemetry(). Never cleared
    within a session — you must restart the kernel to re-enable, matching
    the customer-facing promise that the opt-out cannot be silently reversed.
    """
    return _HARD_OPT_OUT


def set_hard_opt_out() -> None:
    """Turn on the in-process hard opt-out. Idempotent."""
    global _HARD_OPT_OUT
    _HARD_OPT_OUT = True


def _reset_hard_opt_out_for_tests() -> None:
    """TEST-ONLY. Clears the session-scoped hard opt-out flag. Do NOT use
    outside test setUp/tearDown fixtures — customers should never be able
    to reverse an opt-out within a session (see hard_opt_out_active docstring).
    """
    global _HARD_OPT_OUT
    _HARD_OPT_OUT = False


def verify_opt_out_durable() -> bool:
    """After writing the opt-out flag, read it back through the same path
    logic and return True only if the write survived. Used to detect the
    Fabric lakehouse-redirect footgun where writes to /lakehouse/default
    silently land in /tmp/ and disappear on kernel restart.
    """
    try:
        return is_telemetry_opted_out()
    except Exception:
        return False

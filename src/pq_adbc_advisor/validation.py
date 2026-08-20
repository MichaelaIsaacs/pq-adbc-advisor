"""Validation: prove every connector still works after the ADBC switch.

The flow is intentionally read-only:

  1. Customer runs ``scan_workspace()`` BEFORE flipping the ADBC switch.
     We save that baseline.
  2. Customer flips the tenant / workspace ADBC switch (or removes
     Implementation="1.0" pins).
  3. Customer runs ``validate_migration(baseline)`` AFTER the switch.
     We re-list the same artifacts, trigger a fresh refresh on each
     dataset, and compare status + duration.
  4. For every FAILED refresh we classify the error message using the
     rules in ``troubleshoot.py`` and attach a Diagnosis with suggested
     actions.

By default we validate every artifact in the baseline, not just the ones
using a migrating connector - so a customer can see if some unrelated
connector broke as a side effect of the tenant switch.
"""

from __future__ import annotations

import time
from typing import Any

from . import fabric_api, telemetry, troubleshoot
from .report import ImpactReport, ValidationReport, ValidationResult


def validate_migration(
    baseline: ImpactReport,
    access_token: str | None = None,
    trigger_refresh: bool = True,
    only_migrating: bool = False,
    poll_seconds: int = 30,
    max_wait_minutes: int = 30,
    max_parallel: int = 5,
    telemetry_enabled: bool = True,
) -> ValidationReport:
    """Run each artifact through a fresh refresh and classify results.

    Args:
        baseline: The ImpactReport produced by scan_workspace/scan_tenant
            BEFORE the ADBC switch was applied.
        access_token: PBI token; defaults to the notebook user's token.
        trigger_refresh: When True (default) we POST a refresh to each
            semantic model, then poll its refresh history until the
            refresh completes.  Set False if the customer prefers to
            trigger refreshes manually.
        only_migrating: When True, skip artifacts that have no
            migrating connector.  Default False - we validate every
            connector so side effects show up.
        poll_seconds: Poll interval when waiting for a refresh to finish.
        max_wait_minutes: Give up on a refresh after this many minutes.
        max_parallel: Cap on simultaneous refresh polls. Higher = faster
            validation of large workspaces, but consumes more Fabric
            capacity in parallel. Default 5.
    """
    access_token = access_token or fabric_api.get_token()
    report = ValidationReport(baseline_workspace_id=baseline.workspace_id)

    to_validate = [
        a for a in baseline.artifacts
        if not (only_migrating and not a.has_migrating_connector)
    ]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(
                _validate_one, artifact, access_token, trigger_refresh,
                poll_seconds, max_wait_minutes,
            ): artifact
            for artifact in to_validate
        }
        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception as e:
                artifact = futures[fut]
                result = ValidationResult(
                    artifact=artifact,
                    status="failed",
                    reason=f"Internal error during validation: {type(e).__name__}: {e}",
                )
            report.add(result)

    telemetry.emit_validation_summary(report, enabled=telemetry_enabled)
    return report


def _validate_one(
    artifact,
    access_token: str,
    trigger_refresh: bool,
    poll_seconds: int,
    max_wait_minutes: int,
) -> ValidationResult:
    """Run validation for a single artifact. Runs in a worker thread."""
    # We can only actively refresh SemanticModel/Dataset artifacts.
    if artifact.item_type not in ("SemanticModel", "Dataset"):
        return ValidationResult(
            artifact=artifact,
            status="skipped",
            reason=f"validation for {artifact.item_type} not automated",
        )

    pre_history = fabric_api.get_refresh_history(
        artifact.workspace_id, artifact.item_id, access_token, top=1
    )
    pre_last = pre_history[0] if pre_history else None

    if trigger_refresh:
        ok = fabric_api.trigger_refresh(
            artifact.workspace_id, artifact.item_id, access_token
        )
        if not ok:
            return ValidationResult(
                artifact=artifact,
                status="refresh_not_triggered",
                reason="POST to /refreshes was rejected (check permissions)",
                pre_refresh=pre_last,
                diagnosis=troubleshoot.Diagnosis(
                    issue="Insufficient permissions to trigger refresh",
                    likely_cause="Your identity doesn't have Build/Write on this dataset.",
                    suggested_actions=[
                        "Ask a workspace admin to run this notebook, or grant Build permission on the dataset.",
                        "Alternatively, trigger the refresh manually from the Fabric portal and re-run validate_migration(trigger_refresh=False).",
                    ],
                ),
            )
        new_refresh = _wait_for_new_refresh(
            artifact.workspace_id,
            artifact.item_id,
            access_token,
            previous=pre_last,
            poll_seconds=poll_seconds,
            max_wait_minutes=max_wait_minutes,
        )
    else:
        history = fabric_api.get_refresh_history(
            artifact.workspace_id, artifact.item_id, access_token, top=1
        )
        latest = history[0] if history else None
        pre_id = pre_last.get("requestId") if pre_last else None
        latest_id = latest.get("requestId") if latest else None
        if latest is None or (pre_id is not None and pre_id == latest_id):
            return ValidationResult(
                artifact=artifact,
                status="no_new_refresh",
                reason=(
                    "trigger_refresh=False was used but no refresh newer than "
                    "the baseline has completed yet. Trigger a refresh in Fabric, "
                    "then re-run validate_migration."
                ),
                pre_refresh=pre_last,
                diagnosis=troubleshoot.Diagnosis(
                    issue="No new refresh to validate",
                    likely_cause=(
                        "You passed trigger_refresh=False, but the most recent "
                        "refresh in history is the same one recorded when the "
                        "baseline was taken."
                    ),
                    suggested_actions=[
                        "Trigger a refresh manually from the Fabric portal, or",
                        "Re-run with trigger_refresh=True (default).",
                    ],
                ),
            )
        new_refresh = latest

    return _evaluate(artifact, pre_last, new_refresh)


def _wait_for_new_refresh(
    workspace_id: str,
    dataset_id: str,
    access_token: str,
    previous: dict | None,
    poll_seconds: int,
    max_wait_minutes: int,
) -> dict | None:
    deadline = time.time() + max_wait_minutes * 60
    prev_id = previous.get("requestId") if previous else None

    while time.time() < deadline:
        time.sleep(poll_seconds)
        history = fabric_api.get_refresh_history(workspace_id, dataset_id, access_token, top=1)
        if not history:
            continue
        latest = history[0]
        if latest.get("requestId") == prev_id:
            continue
        status = latest.get("status")
        if status in ("Completed", "Failed", "Disabled"):
            return latest
    return None


def _evaluate(artifact, pre, post) -> ValidationResult:
    if post is None:
        return ValidationResult(
            artifact=artifact,
            status="no_new_refresh",
            reason="No refresh completed within the wait window",
            pre_refresh=pre,
            diagnosis=troubleshoot.Diagnosis(
                issue="Refresh did not finish in time",
                likely_cause="The refresh took longer than max_wait_minutes, or was still queued when we stopped polling.",
                suggested_actions=[
                    "Re-run validate_migration with a larger max_wait_minutes.",
                    "Check the refresh history in the Fabric portal to see if it eventually completed.",
                    "For very large models, consider validating with only_migrating=True first.",
                ],
            ),
        )

    post_status = post.get("status")
    if post_status == "Completed":
        pre_dur = _duration_seconds(pre) if pre else None
        post_dur = _duration_seconds(post)
        delta = None
        if pre_dur and post_dur:
            delta = round((post_dur - pre_dur) / max(pre_dur, 1) * 100.0, 1)
        reason = "Refresh completed successfully after ADBC switch"
        # Regression check per External Guide #7
        regression_diag = troubleshoot.diagnose_performance_regression(delta)
        if regression_diag is not None:
            reason += f" (regression: {delta:+.1f}% slower than baseline)"
        return ValidationResult(
            artifact=artifact,
            status="passed_with_regression" if regression_diag else "passed",
            reason=reason,
            pre_refresh=pre,
            post_refresh=post,
            duration_delta_pct=delta,
            diagnosis=regression_diag,
        )

    # Failed / Disabled / Unknown
    error_text = post.get("serviceExceptionJson") or f"Refresh status: {post_status}"
    diag = troubleshoot.diagnose(error_text)
    return ValidationResult(
        artifact=artifact,
        status="failed",
        reason=error_text[:600],
        pre_refresh=pre,
        post_refresh=post,
        diagnosis=diag,
    )


def _duration_seconds(refresh: dict) -> float | None:
    start = refresh.get("startTime")
    end = refresh.get("endTime")
    if not (start and end):
        return None
    from datetime import datetime
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return (e - s).total_seconds()
    except Exception:
        return None

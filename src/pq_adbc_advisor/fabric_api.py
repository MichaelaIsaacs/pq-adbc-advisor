"""Thin wrappers around the Fabric / Power BI REST APIs used by the advisor.

Nearly everything here is READ-ONLY.  The one exception is
``trigger_refresh`` which POSTs a refresh request; it is only called from
``validation.py`` and only when the caller opts in (``trigger_refresh=True``
is the default of ``validate_migration``, but the scan phase never calls it).

Auth strategy: reuse the notebook user's token via
``notebookutils.credentials.getToken("pbi")`` when running inside Fabric.
For local runs, callers may pass an ``access_token`` explicitly.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from .constants import (
    SCANNER_CHUNK_SIZE,
    SCANNER_MAX_POLLS,
    SCANNER_POLL_INTERVAL_SEC,
)

_FABRIC_API = "https://api.fabric.microsoft.com/v1"
_PBI_API = "https://api.powerbi.com/v1.0/myorg"


def get_token() -> str:
    """Return a Power BI access token.

    Prefers the Fabric notebook helper when available; falls back to raising
    a clear error otherwise so the caller can supply a token explicitly.
    """
    try:
        import notebookutils  # type: ignore
        return notebookutils.credentials.getToken("pbi")
    except Exception as e:
        raise RuntimeError(
            "Could not acquire a Power BI token via notebookutils. "
            "Pass access_token= explicitly when calling scan_workspace / scan_tenant."
        ) from e


def _auth_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _bearer(access_token: str) -> dict[str, str]:
    """Return a bearer-only Authorization header (no Content-Type)."""
    return {"Authorization": f"Bearer {access_token}"}


def current_workspace_id() -> str:
    """Return the workspace ID the notebook is running in.

    Only works inside a Fabric notebook.
    """
    try:
        from pyspark.sql import SparkSession  # type: ignore
        spark = SparkSession.builder.getOrCreate()
        return spark.conf.get("trident.workspace.id")
    except Exception as e:
        raise RuntimeError(
            "Could not detect the current workspace ID. "
            "Pass workspace_id= explicitly."
        ) from e


# --------------------------------------------------------------------------- #
# Fabric items API - list items and fetch definitions in a single workspace
# --------------------------------------------------------------------------- #

def list_items(workspace_id: str, access_token: str, item_type: str | None = None) -> list[dict[str, Any]]:
    """Return every item in a workspace (semantic models, dataflows, pipelines...).

    Handles both continuationUri (older) and continuationToken (newer) paging.
    """
    items: list[dict[str, Any]] = []
    base_url = f"{_FABRIC_API}/workspaces/{workspace_id}/items"
    if item_type:
        base_url += f"?type={item_type}"
    url = base_url
    while url:
        r = requests.get(url, headers=_auth_headers(access_token))
        r.raise_for_status()
        body = r.json()
        items.extend(body.get("value", []))
        # Fabric APIs use one of these for paging:
        next_url = body.get("continuationUri")
        if not next_url:
            token = body.get("continuationToken")
            if token:
                # Preserve query params (e.g. ?type=SemanticModel) when we go to the next page.
                sep = "&" if "?" in base_url else "?"
                next_url = f"{base_url}{sep}continuationToken={token}"
        url = next_url
    return items


def get_item_definition(
    workspace_id: str,
    item_id: str,
    access_token: str,
    item_type: str | None = None,
) -> dict[str, Any] | None:
    """Fetch the item's definition (base64 parts) via the Fabric REST API.

    Returns None if the item type does not support getDefinition.
    Handles the 202 long-running-operation pattern.

    For SemanticModel we request format=TMSL because our JSON scanner is
    more reliable than the TMDL heuristic.  For other item types we let
    the API pick the default format.
    """
    url = f"{_FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/getDefinition"
    if item_type in ("SemanticModel", "Dataset"):
        url += "?format=TMSL"

    r = requests.post(url, headers=_auth_headers(access_token))
    if r.status_code == 400:
        # Unsupported item type or unsupported format -> treat as no-def.
        return None
    if r.status_code == 202:
        return _await_lro(r, access_token)
    if r.status_code >= 400:
        return None
    return r.json()


def _await_lro(initial_response: requests.Response, access_token: str) -> dict[str, Any] | None:
    """Poll a Fabric long-running operation until it completes or times out."""
    location = initial_response.headers.get("Location")
    if not location:
        return None

    # Use the initial Retry-After only for the first sleep; each subsequent poll
    # may return its own hint via the poll response headers.
    next_retry = int(initial_response.headers.get("Retry-After", SCANNER_POLL_INTERVAL_SEC))

    for _ in range(SCANNER_MAX_POLLS):
        time.sleep(next_retry)
        auth = _bearer(access_token)
        poll = requests.get(location, headers=auth)
        if poll.status_code >= 400:
            return None
        # Update backoff hint for next iteration
        next_retry = int(poll.headers.get("Retry-After", SCANNER_POLL_INTERVAL_SEC))
        data = poll.json()
        status = data.get("status")
        if status == "Succeeded":
            result_url = poll.headers.get("Location") or location + "/result"
            final = requests.get(result_url, headers=auth)
            if final.status_code < 400:
                return final.json()
            return None
        if status in ("Failed", "Cancelled"):
            return None
    return None


# --------------------------------------------------------------------------- #
# Power BI Scanner API - discover artifacts + connectors across many workspaces
# --------------------------------------------------------------------------- #

def scan_workspaces_modified(access_token: str, exclude_personal: bool = True) -> list[dict]:
    """Return the list of workspaces the scanner API knows about."""
    url = (
        f"{_PBI_API.replace('/v1.0/myorg', '')}/v1.0/myorg/admin/workspaces/modified"
        f"?excludePersonalWorkspaces={str(exclude_personal).lower()}"
    )
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"})
    r.raise_for_status()
    data = r.json()
    return data.get("value", data) if isinstance(data, dict) else data


def _start_scan(access_token: str, workspace_ids: list[str]) -> str:
    """Kick off an admin scan for a chunk of workspace IDs; returns the poll URL."""
    url = (
        f"{_PBI_API}/admin/workspaces/getInfo"
        "?datasetExpressions=True&datasetSchema=True"
        "&datasourceDetails=True&getArtifactUsers=False&lineage=True"
    )
    r = requests.post(
        url,
        headers=_auth_headers(access_token),
        json={"workspaces": workspace_ids},
    )
    r.raise_for_status()
    return r.headers["Location"]


def _poll_scan(access_token: str, poll_url: str) -> str:
    for _ in range(SCANNER_MAX_POLLS):
        time.sleep(SCANNER_POLL_INTERVAL_SEC)
        r = requests.get(poll_url, headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        if r.json().get("status") == "Succeeded":
            return poll_url.replace("/scanStatus/", "/scanResult/")
    raise TimeoutError(f"Scanner API poll timed out for {poll_url}")


def scan_tenant_workspaces(access_token: str, workspace_ids: list[str]) -> dict[str, Any]:
    """Run the admin Scanner API against a subset of workspaces.

    Requires tenant-admin rights (or a service principal in the correct
    security group).  Returns the merged 'workspaces' payload.
    """
    chunks = [
        workspace_ids[i : i + SCANNER_CHUNK_SIZE]
        for i in range(0, len(workspace_ids), SCANNER_CHUNK_SIZE)
    ]
    poll_urls = [_start_scan(access_token, chunk) for chunk in chunks]

    merged: dict[str, Any] = {"workspaces": []}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_poll_scan, access_token, url): url for url in poll_urls}
        for fut in as_completed(futures):
            result_url = fut.result()
            r = requests.get(result_url, headers={"Authorization": f"Bearer {access_token}"})
            r.raise_for_status()
            merged["workspaces"].extend(r.json().get("workspaces", []))
    return merged


# --------------------------------------------------------------------------- #
# Refresh + gateway helpers used by validation.py
# --------------------------------------------------------------------------- #

def get_refresh_history(workspace_id: str, dataset_id: str, access_token: str, top: int = 5) -> list[dict]:
    url = f"{_PBI_API}/groups/{workspace_id}/datasets/{dataset_id}/refreshes?$top={top}"
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code >= 400:
        return []
    return r.json().get("value", [])


def get_dataset_gateway(workspace_id: str, dataset_id: str, access_token: str) -> str | tuple[None, str] | None:
    """Return the gatewayId bound to a dataset.

    Returns:
        - A gateway ID string when a gateway is bound
        - None when we successfully saw the datasource list and no gateway is bound
        - The special string "unknown" when we could not read the datasource
          list (401/403/network etc.).  Callers should distinguish this from
          a definite "no gateway" to avoid mis-classifying risk as HIGH.
    """
    url = f"{_PBI_API}/groups/{workspace_id}/datasets/{dataset_id}/datasources"
    r = requests.get(url, headers=_bearer(access_token))
    if r.status_code >= 400:
        return "unknown"
    try:
        values = r.json().get("value", [])
    except ValueError:
        return "unknown"
    for ds in values:
        gw = ds.get("gatewayId")
        if gw:
            return gw
    return None


def trigger_refresh(workspace_id: str, dataset_id: str, access_token: str) -> bool:
    """POST a refresh request. Returns True if accepted."""
    url = f"{_PBI_API}/groups/{workspace_id}/datasets/{dataset_id}/refreshes"
    r = requests.post(
        url,
        headers=_auth_headers(access_token),
        json={"notifyOption": "NoNotification"},
    )
    return r.status_code in (200, 202)


# --------------------------------------------------------------------------- #
# Fabric Connections (first-class shared connections in the Fabric portal)
# --------------------------------------------------------------------------- #

def list_fabric_connections(access_token: str) -> tuple[list[dict[str, Any]], str | None]:
    """Enumerate the caller's Fabric Connections via the Fabric REST API.

    Returns a tuple ``(connections, error)``:
      * ``connections`` — the list of visible connections (possibly empty).
      * ``error`` — None on success, or a short string describing why the
        API was unreachable ("http 403", "http 401", "network", ...).

    Some tenants block this API for non-admins; callers should surface the
    error string so an "empty" list isn't confused with "no connections".
    """
    connections: list[dict[str, Any]] = []
    url = f"{_FABRIC_API}/connections"
    while url:
        try:
            r = requests.get(url, headers=_auth_headers(access_token))
        except requests.RequestException as e:
            return connections, f"network: {type(e).__name__}"
        if r.status_code >= 400:
            return connections, f"http {r.status_code}"
        try:
            body = r.json()
        except ValueError:
            return connections, "non-json response"
        connections.extend(body.get("value", []))
        url = body.get("continuationUri")
    return connections, None


def list_data_pipelines(workspace_id: str, access_token: str) -> list[dict[str, Any]]:
    """List Fabric Data Pipeline items in a workspace."""
    url = f"{_FABRIC_API}/workspaces/{workspace_id}/dataPipelines"
    r = requests.get(url, headers=_auth_headers(access_token))
    if r.status_code >= 400:
        return []
    return r.json().get("value", [])


def get_dataset_refresh_error(
    workspace_id: str, dataset_id: str, access_token: str, request_id: str | None = None
) -> str | None:
    """Return the raw error message for the most recent (or specified) refresh.

    The refresh history endpoint returns 'serviceExceptionJson' for failed
    refreshes; we surface that string so troubleshoot.diagnose can classify
    it.
    """
    history = get_refresh_history(workspace_id, dataset_id, access_token, top=5)
    if not history:
        return None
    target = history[0]
    if request_id:
        for entry in history:
            if entry.get("requestId") == request_id:
                target = entry
                break
    exc = target.get("serviceExceptionJson")
    if exc:
        return exc
    if target.get("status") == "Failed":
        return f"Refresh failed at {target.get('endTime')} (no detail returned)"
    return None

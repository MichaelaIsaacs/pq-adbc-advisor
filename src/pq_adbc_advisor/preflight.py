"""Preflight self-check to catch API contract mismatches before a real scan.

Customer runs::

    from pq_adbc_advisor import preflight_check
    preflight_check()

It probes each REST endpoint the advisor depends on and prints a checklist
with green/red status.  Zero side effects - no writes, no refreshes.
"""

from __future__ import annotations

from typing import Any

import requests

from . import fabric_api


CHECKS = [
    "token_acquisition",
    "workspace_id_detection",
    "list_items",
    "list_semantic_models",
    "get_semantic_model_definition",
    "get_dataset_datasources",
    "list_fabric_connections",
    "get_refresh_history",
]


def preflight_check(
    workspace_id: str | None = None,
    access_token: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Probe each API this library uses. Return a status dict."""
    results: dict[str, dict[str, Any]] = {}

    # ---- 1) Token --------------------------------------------------------
    try:
        access_token = access_token or fabric_api.get_token()
        results["token_acquisition"] = {"ok": True, "detail": "acquired"}
    except Exception as e:
        results["token_acquisition"] = {"ok": False, "detail": str(e)}
        _print_results(results, verbose)
        return results

    # ---- 2) Workspace ID -------------------------------------------------
    try:
        workspace_id = workspace_id or fabric_api.current_workspace_id()
        results["workspace_id_detection"] = {"ok": True, "detail": workspace_id}
    except Exception as e:
        results["workspace_id_detection"] = {"ok": False, "detail": str(e)}
        _print_results(results, verbose)
        return results

    # ---- 3) List items ---------------------------------------------------
    try:
        items = fabric_api.list_items(workspace_id, access_token)
        results["list_items"] = {"ok": True, "detail": f"{len(items)} items"}
    except Exception as e:
        results["list_items"] = {"ok": False, "detail": _short(e)}
        items = []

    # ---- 4) List semantic models specifically ---------------------------
    try:
        models = fabric_api.list_items(workspace_id, access_token, item_type="SemanticModel")
        results["list_semantic_models"] = {"ok": True, "detail": f"{len(models)} SemanticModel items"}
    except Exception as e:
        results["list_semantic_models"] = {"ok": False, "detail": _short(e)}
        models = []

    # ---- 5) Get one semantic model definition ----------------------------
    if models:
        sample = models[0]
        try:
            defn = fabric_api.get_item_definition(
                workspace_id, sample["id"], access_token, item_type="SemanticModel"
            )
            if defn is None:
                results["get_semantic_model_definition"] = {
                    "ok": False,
                    "detail": f"getDefinition returned None for '{sample.get('displayName')}'",
                }
            else:
                parts = (defn.get("definition") or {}).get("parts", [])
                paths = [p.get("path", "") for p in parts]
                results["get_semantic_model_definition"] = {
                    "ok": True,
                    "detail": f"{len(parts)} parts: {paths[:3]}...",
                }
        except Exception as e:
            results["get_semantic_model_definition"] = {"ok": False, "detail": _short(e)}
    else:
        results["get_semantic_model_definition"] = {
            "ok": None,
            "detail": "no semantic models in workspace - skipped",
        }

    # ---- 6) Get datasources (used for gateway detection) -----------------
    if models:
        sample = models[0]
        try:
            gw = fabric_api.get_dataset_gateway(workspace_id, sample["id"], access_token)
            results["get_dataset_datasources"] = {
                "ok": True,
                "detail": f"gateway={gw!r}",
            }
        except Exception as e:
            results["get_dataset_datasources"] = {"ok": False, "detail": _short(e)}
    else:
        results["get_dataset_datasources"] = {"ok": None, "detail": "skipped"}

    # ---- 7) Fabric Connections ------------------------------------------
    try:
        conns, err = fabric_api.list_fabric_connections(access_token)
        if err:
            results["list_fabric_connections"] = {
                "ok": False,
                "detail": f"{err} (this API is often blocked for non-admins)",
            }
        else:
            results["list_fabric_connections"] = {
                "ok": True,
                "detail": f"{len(conns)} connections visible",
            }
    except Exception as e:
        results["list_fabric_connections"] = {"ok": False, "detail": _short(e)}

    # ---- 8) Refresh history ---------------------------------------------
    if models:
        sample = models[0]
        try:
            hist = fabric_api.get_refresh_history(
                workspace_id, sample["id"], access_token, top=1
            )
            results["get_refresh_history"] = {
                "ok": True,
                "detail": f"{len(hist)} history entries",
            }
        except Exception as e:
            results["get_refresh_history"] = {"ok": False, "detail": _short(e)}
    else:
        results["get_refresh_history"] = {"ok": None, "detail": "skipped"}

    _print_results(results, verbose)
    return results


def _short(e: Exception) -> str:
    text = str(e)
    return text[:200]


def _print_results(results: dict[str, dict[str, Any]], verbose: bool) -> None:
    if not verbose:
        return
    print("\n" + "=" * 70)
    print("pq-adbc-advisor preflight check")
    print("=" * 70)
    for check in CHECKS:
        r = results.get(check)
        if r is None:
            symbol = "?"
            detail = "not run"
        elif r["ok"] is True:
            symbol = "OK "
            detail = r["detail"]
        elif r["ok"] is False:
            symbol = "FAIL"
            detail = r["detail"]
        else:
            symbol = "-- "
            detail = r["detail"]
        print(f"  [{symbol:>4}] {check:35s} {detail}")
    print("=" * 70)

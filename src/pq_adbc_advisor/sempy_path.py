"""Sempy.fabric fast path for semantic model M expression extraction.

Why: Fabric's REST getDefinition endpoint is a Long-Running Operation.
Even after v0.2.3's LRO tuning, each artifact still incurs one HTTP
handshake + one Location poll. sempy.fabric — the same library Pat
Mahoney's DFG2 Migration Accelerator uses — can pull a semantic model's
tables and expressions directly from the workspace without the LRO
handshake, and returns pandas frames the caller can already reason
about.

Design constraints
------------------
1. **Optional dependency.** sempy is only shipped in Fabric notebooks.
   Local dev environments (macOS, CI) will not have it and must not
   fail import.
2. **Silent fallback.** When sempy is unavailable, we return None and
   discovery.py falls back to the REST path. No warnings, no errors —
   the fallback is expected on any non-Fabric runtime.
3. **Compatible output shape.** The function returns the SAME shape as
   ``definitions.extract_m_expressions``: ``list[{"name", "expression"}]``.
4. **Sempy API is not versioned identically across releases.** We probe
   a couple of possible attribute names and swallow AttributeError so
   an older/newer sempy build doesn't crash the scan — we just fall
   back to REST for that artifact.

Coverage
--------
Currently only ``SemanticModel`` / ``Dataset`` items benefit from
sempy. Dataflows still go through the REST getDefinition path because
sempy has no equivalent for them.
"""

from __future__ import annotations

from typing import Any


def sempy_available() -> bool:
    """Return True if sempy.fabric can be imported in the current runtime."""
    try:
        import sempy.fabric  # noqa: F401
        return True
    except Exception:
        return False


def extract_semantic_model_expressions_via_sempy(
    workspace_id: str,
    dataset_id: str,
    dataset_name: str | None = None,
) -> list[dict] | None:
    """Return M expressions for a semantic model using sempy.fabric.

    Returns:
        A list of ``{"name", "expression"}`` dicts on success, or None
        if sempy isn't available, the model can't be reached, or the
        model exposes no M partitions (in which case the caller should
        fall back to REST rather than assume "no expressions").
    """
    try:
        import sempy.fabric as fabric
    except Exception:
        return None

    identifier: Any = dataset_name or dataset_id
    out: list[dict] = []

    # Attempt 1: list_partitions gives us name + source_type + source_expression
    try:
        parts = fabric.list_partitions(dataset=identifier, workspace=workspace_id)
        # DataFrame columns vary by sempy version; probe defensively.
        cols = {c.lower(): c for c in parts.columns}
        expr_col = cols.get("source expression") or cols.get("source_expression") or cols.get("expression")
        type_col = cols.get("source type") or cols.get("source_type")
        name_col = cols.get("partition name") or cols.get("name")
        table_col = cols.get("table name") or cols.get("table")
        if expr_col:
            for _, row in parts.iterrows():
                if type_col and str(row.get(type_col, "")).lower() not in ("m", "mashup", "powerquery"):
                    continue
                expression = row.get(expr_col)
                if not isinstance(expression, str) or not expression.strip():
                    continue
                tbl = row.get(table_col) if table_col else "?"
                pn = row.get(name_col) if name_col else ""
                out.append({"name": f"{tbl}.{pn}".rstrip("."), "expression": expression})
    except Exception:
        # Any failure (permissions, API drift, missing method) => fall back.
        return None

    # Attempt 2: list_expressions for shared M expressions
    try:
        exprs = fabric.list_expressions(dataset=identifier, workspace=workspace_id)
        cols = {c.lower(): c for c in exprs.columns}
        expr_col = cols.get("expression") or cols.get("m") or cols.get("expression text")
        name_col = cols.get("name")
        if expr_col:
            for _, row in exprs.iterrows():
                expression = row.get(expr_col)
                if isinstance(expression, str) and expression.strip():
                    nm = row.get(name_col) if name_col else "?"
                    out.append({"name": f"shared:{nm}", "expression": expression})
    except Exception:
        # list_expressions may not exist on older sempy; ignore.
        pass

    return out or None

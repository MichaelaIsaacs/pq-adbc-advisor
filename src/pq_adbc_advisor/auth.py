"""Authentication paths for pq-adbc-advisor.

Three ways to acquire a Power BI / Fabric access token, in order of
precedence:

1. **Explicit ``access_token`` kwarg** to ``scan_workspace`` /
   ``scan_tenant`` / ``validate_migration``. Highest precedence — the
   caller has already resolved auth however they want.

2. **Service Principal environment variables** (v0.3.0). When
   ``PQ_ADBC_ADVISOR_SP_TENANT_ID`` + ``PQ_ADBC_ADVISOR_SP_CLIENT_ID``
   + (``PQ_ADBC_ADVISOR_SP_CLIENT_SECRET`` OR
   ``PQ_ADBC_ADVISOR_SP_CERT_PATH``) are all set we acquire a token via
   MSAL's ``ConfidentialClientApplication``. This lets a scheduled
   pipeline run the scan against a service principal that has been
   granted Fabric admin / workspace admin rights.

3. **Delegated notebook user** — the default. Uses
   ``notebookutils.credentials.getToken("pbi")`` inside a Fabric
   notebook. Falls through to a clear error message outside Fabric.

Design constraints
------------------
* ``msal`` is an OPTIONAL dependency. If it isn't installed the SP path
  raises a clear ``ImportError`` telling the caller to run
  ``pip install pq-adbc-advisor[sp]``.
* We do NOT persist tokens. They live in the process for the duration
  of the scan and are discarded.
* Certificate auth uses PEM on disk (path passed via env). Not passing
  a PFX blob to keep the surface small — Fabric Toolbox / customer
  documentation recommends PEM.
"""

from __future__ import annotations

import os
from typing import Any


_PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

# Env var names — exposed so callers can construct their own env
# rather than hard-coding strings.
ENV_TENANT = "PQ_ADBC_ADVISOR_SP_TENANT_ID"
ENV_CLIENT = "PQ_ADBC_ADVISOR_SP_CLIENT_ID"
ENV_SECRET = "PQ_ADBC_ADVISOR_SP_CLIENT_SECRET"
ENV_CERT_PATH = "PQ_ADBC_ADVISOR_SP_CERT_PATH"
ENV_CERT_THUMB = "PQ_ADBC_ADVISOR_SP_CERT_THUMBPRINT"


def service_principal_env_set() -> bool:
    """Return True when the SP env vars are all present."""
    tenant = os.environ.get(ENV_TENANT)
    client = os.environ.get(ENV_CLIENT)
    if not (tenant and client):
        return False
    if os.environ.get(ENV_SECRET):
        return True
    if os.environ.get(ENV_CERT_PATH):
        return True
    return False


def acquire_token_service_principal(
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    cert_path: str | None = None,
    cert_thumbprint: str | None = None,
) -> str:
    """Acquire a Power BI access token via MSAL confidential-client flow.

    Args:
        tenant_id: AAD tenant ID. Falls back to ``PQ_ADBC_ADVISOR_SP_TENANT_ID``.
        client_id: SP app registration ID. Falls back to
            ``PQ_ADBC_ADVISOR_SP_CLIENT_ID``.
        client_secret: SP client secret. Falls back to
            ``PQ_ADBC_ADVISOR_SP_CLIENT_SECRET``. Prefer certificate
            auth for production.
        cert_path: Path to a PEM file. Falls back to
            ``PQ_ADBC_ADVISOR_SP_CERT_PATH``.
        cert_thumbprint: The SHA-1 thumbprint of the cert. Falls back
            to ``PQ_ADBC_ADVISOR_SP_CERT_THUMBPRINT``. If not supplied
            and ``cryptography`` is installed we compute it from the
            PEM automatically.

    Returns:
        A bearer token string suitable for the Authorization header.

    Raises:
        ImportError: msal is not installed.
        RuntimeError: MSAL declined to issue a token (bad creds, missing
            admin consent, wrong scope, etc.).
    """
    try:
        import msal  # type: ignore
    except ImportError as e:
        raise ImportError(
            "The service-principal auth path requires msal. Install with: "
            "pip install msal   (or   pip install pq-adbc-advisor[sp])"
        ) from e

    tenant_id = tenant_id or os.environ.get(ENV_TENANT)
    client_id = client_id or os.environ.get(ENV_CLIENT)
    client_secret = client_secret or os.environ.get(ENV_SECRET)
    cert_path = cert_path or os.environ.get(ENV_CERT_PATH)
    cert_thumbprint = cert_thumbprint or os.environ.get(ENV_CERT_THUMB)

    if not (tenant_id and client_id):
        raise RuntimeError(
            f"Service principal auth requires {ENV_TENANT} and {ENV_CLIENT} "
            "either as kwargs or environment variables."
        )

    authority = f"https://login.microsoftonline.com/{tenant_id}"

    credential: Any
    if client_secret:
        credential = client_secret
    elif cert_path:
        with open(cert_path, "rb") as f:
            pem = f.read()
        if not cert_thumbprint:
            cert_thumbprint = _thumbprint_from_pem(pem)
        credential = {"private_key": pem.decode(), "thumbprint": cert_thumbprint}
    else:
        raise RuntimeError(
            f"Service principal auth requires either {ENV_SECRET} or "
            f"{ENV_CERT_PATH} (with {ENV_CERT_THUMB} unless cryptography is installed)."
        )

    app = msal.ConfidentialClientApplication(
        client_id, authority=authority, client_credential=credential,
    )
    result = app.acquire_token_for_client(scopes=[_PBI_SCOPE])
    if "access_token" not in result:
        raise RuntimeError(
            f"MSAL did not return an access token: "
            f"{result.get('error')} / {result.get('error_description')}"
        )
    return result["access_token"]


def _thumbprint_from_pem(pem_bytes: bytes) -> str:
    """Derive the SHA-1 thumbprint from a PEM cert. Requires cryptography."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization  # noqa: F401
        cert = x509.load_pem_x509_certificate(pem_bytes)
        return cert.fingerprint(hashes.SHA1()).hex().upper()
    except ImportError as e:
        raise RuntimeError(
            "Certificate thumbprint not provided and 'cryptography' is not installed. "
            f"Either set {ENV_CERT_THUMB} explicitly or install cryptography."
        ) from e

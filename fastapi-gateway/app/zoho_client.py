"""
Minimal async client for the Zoho CRM REST API (Contacts search only).

This exists so `GET /data/subject/{email}` — the "raw" no-Fides-in-the-path
lookup that already reads app-postgres and app-mongo directly (see db.py) —
can ALSO check Zoho CRM directly, the same way. Fides' own SaaS connector
(fides-config/saas/zoho_crm/) is a separate, parallel path used only by real
DSARs (POST /dsar); this client duplicates just enough of its logic (same
endpoint, same auth header, same 204-means-no-match handling) to answer "does
this email exist in Zoho" without going through a Fides privacy request.

Zoho access tokens expire hourly; the refresh token does not, so this client
refreshes lazily on first use and again on any 401, rather than on a timer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("gateway.zoho")

# Same field list as fides-config/saas/zoho_crm/dataset.yml, so this lookup
# and a real Fides access request return the same shape of contact record.
CONTACT_FIELDS = ["id", "Email", "First_Name", "Last_Name", "Phone"]


class ZohoError(RuntimeError):
    """A non-success response from Zoho's API."""


class ZohoClient:
    """`None` domain/client_id/client_secret/refresh_token means "not
    configured" — every method then raises `ZohoError` immediately rather than
    the gateway failing to start. That keeps a missing Zoho setup a per-call
    error (surfaced in `note`/`found_in`), not a boot-time crash.
    """

    def __init__(
        self,
        accounts_domain: str | None,
        api_domain: str | None,
        client_id: str | None,
        client_secret: str | None,
        refresh_token: str | None,
        timeout: float = 15.0,
    ) -> None:
        self.accounts_domain = accounts_domain
        self.api_domain = api_domain
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(
            self.accounts_domain
            and self.api_domain
            and self._client_id
            and self._client_secret
            and self._refresh_token
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- auth --
    async def _refresh_access_token(self) -> str:
        """POST to Zoho's own OAuth endpoint (not Fides) for a fresh access
        token, exactly like provision.py's refresh_zoho_access_token()."""
        resp = await self._client.post(
            f"https://{self.accounts_domain}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
        )
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        token = body.get("access_token")
        if resp.status_code != 200 or not token:
            raise ZohoError(f"Zoho token refresh failed: HTTP {resp.status_code} {resp.text}")
        logger.info("Zoho CRM access token refreshed")
        return token

    async def _get_token(self, force: bool = False) -> str:
        async with self._lock:
            if self._access_token is None or force:
                self._access_token = await self._refresh_access_token()
            return self._access_token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self._get_token()
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        resp = await self._client.request(
            method, f"https://{self.api_domain}{path}", headers=headers, **kwargs
        )
        if resp.status_code == 401:
            # Access token expired early or was revoked — refresh once and retry.
            token = await self._get_token(force=True)
            headers = {"Authorization": f"Zoho-oauthtoken {token}"}
            resp = await self._client.request(
                method, f"https://{self.api_domain}{path}", headers=headers, **kwargs
            )
        return resp

    # -------------------------------------------------------------- contacts --
    async def find_contacts_by_email(self, email: str) -> list[dict[str, Any]]:
        """`GET /crm/v6/Contacts/search?email=<email>`.

        Zoho returns 204 (no body) when nothing matches — that is success, not
        an error, exactly as fides-config/saas/zoho_crm/config.yml's
        `ignore_errors: [204]` documents for the real connector.
        """
        if not self.configured:
            raise ZohoError(
                "Zoho CRM is not configured (ZOHO_CRM_* environment variables "
                "are missing), so this lookup was skipped."
            )
        resp = await self._request(
            "GET",
            "/crm/v6/Contacts/search",
            params={"email": email, "fields": ",".join(CONTACT_FIELDS)},
        )
        if resp.status_code == 204:
            return []
        if resp.status_code != 200:
            raise ZohoError(f"Zoho CRM search failed: HTTP {resp.status_code} {resp.text}")
        body = resp.json()
        return [
            {field: record.get(field) for field in CONTACT_FIELDS}
            for record in body.get("data", [])
        ]

    async def ping(self) -> str:
        """`GET /crm/v6/org` — same path Fides' own `test_request` uses to
        validate the connection. Used by /health."""
        if not self.configured:
            return "not configured"
        try:
            resp = await self._request("GET", "/crm/v6/org")
        except Exception as exc:  # noqa: BLE001 - surfaced as a health status string
            return f"unreachable: {exc}"
        return "ok" if resp.status_code == 200 else f"HTTP {resp.status_code}"

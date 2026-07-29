"""
Async client for the Fides API.

Handles the bits you don't want to redo on every call:
  * logging in with the root credentials and caching the bearer token,
  * transparently re-authenticating once on a 401 (tokens expire),
  * normalising Fides' several different privacy-request read endpoints into
    one object the gateway can return.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("gateway.fides")


class FidesError(RuntimeError):
    """A non-success response from the Fides API."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"Fides API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class FidesClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v1"
        self._username = username
        self._password = password
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- auth --
    async def _login(self) -> str:
        """POST /api/v1/login -> token_data.access_token"""
        resp = await self._client.post(
            f"{self.api}/login",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise FidesError(resp.status_code, _detail(resp))
        token = resp.json()["token_data"]["access_token"]
        logger.info("authenticated against Fides as '%s'", self._username)
        return token

    async def _get_token(self, force: bool = False) -> str:
        async with self._lock:
            if self._token is None or force:
                self._token = await self._login()
            return self._token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Authenticated request, retrying once after a fresh login on 401."""
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self._client.request(
            method, f"{self.api}{path}", headers=headers, **kwargs
        )
        if resp.status_code == 401:
            logger.info("Fides token rejected; re-authenticating")
            token = await self._get_token(force=True)
            headers = {"Authorization": f"Bearer {token}"}
            resp = await self._client.request(
                method, f"{self.api}{path}", headers=headers, **kwargs
            )
        return resp

    async def _json(self, method: str, path: str, ok: tuple[int, ...] = (200,), **kw: Any) -> Any:
        resp = await self._request(method, path, **kw)
        if resp.status_code not in ok:
            raise FidesError(resp.status_code, _detail(resp))
        return resp.json()

    # -------------------------------------------------------------- health --
    async def health(self) -> dict:
        """GET /health — unauthenticated, so it works even if creds are wrong."""
        resp = await self._client.get(f"{self.base_url}/health")
        if resp.status_code != 200:
            raise FidesError(resp.status_code, _detail(resp))
        return resp.json()

    # ----------------------------------------------------- privacy requests --
    async def create_privacy_request(self, email: str, policy_key: str) -> dict:
        """POST /api/v1/privacy-request

        Body is a LIST — Fides accepts a batch. It returns
        {"succeeded": [...], "failed": [...]} and answers 200 even when the
        single item failed, so the `failed` list has to be checked explicitly.
        """
        body = [{"identity": {"email": email}, "policy_key": policy_key}]
        result = await self._json("POST", "/privacy-request", json=body)

        failed = result.get("failed") or []
        if failed:
            raise FidesError(400, failed[0].get("message", failed))

        succeeded = result.get("succeeded") or []
        if not succeeded:
            raise FidesError(500, "Fides accepted the request but returned no id")
        return succeeded[0]

    async def get_privacy_request(self, request_id: str) -> dict | None:
        """POST /api/v1/privacy-request/search

        The old `GET /privacy-request?request_id=` filter is deprecated; search
        is the current way to look one up by id.
        """
        result = await self._json(
            "POST", "/privacy-request/search", json={"request_id": request_id}
        )
        items = result.get("items") or []
        return items[0] if items else None

    async def get_execution_logs(self, request_id: str) -> list[dict]:
        """GET /api/v1/privacy-request/{id}/log

        This is the per-collection audit trail: one entry per dataset:collection
        per action, with status and the fields it touched. This is the artifact
        a regulator would ask for.
        """
        result = await self._json("GET", f"/privacy-request/{request_id}/log")
        # Paginated on some versions, a bare list on others.
        if isinstance(result, dict):
            return result.get("items") or []
        return result or []

    async def get_access_results(self, request_id: str) -> dict | None:
        """GET /api/v1/privacy-request/{id}/access-results

        Returns where the access package went, NOT the rows. With an S3/GCS
        storage destination `access_result_urls` holds presigned download URLs;
        with the `local` destination used here it is the literal string
        "your local fides_uploads folder" — so the gateway reads the package off
        disk itself (see main.py::_read_access_package).

        Requires `security.subject_request_download_ui_enabled = true` in
        fides.toml. Without it this endpoint answers
        403 "Access results download is disabled."

        NOTE: `GET /privacy-request/{id}/filtered-results` looks like the
        endpoint you want for inline data, and it is what older guides use, but
        as of Fides 2.8x it is restricted to *test* privacy requests:
        403 "Results can only be retrieved for test privacy requests."
        """
        resp = await self._request("GET", f"/privacy-request/{request_id}/access-results")
        if resp.status_code == 200:
            return resp.json()
        return {"unavailable": _detail(resp), "status_code": resp.status_code}


def _detail(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text

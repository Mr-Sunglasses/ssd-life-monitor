"""Small synchronous client for the collector Unix-socket API."""

from __future__ import annotations

import os
from typing import Any

import httpx


class CollectorUnavailable(RuntimeError):
    """The privileged collector could not serve a request."""


class CollectorClient:
    def __init__(self, socket_path: str | None = None, timeout: float = 10.0):
        self.socket_path = socket_path or os.getenv(
            "COLLECTOR_SOCKET", "/run/ssd-life/collector.sock"
        )
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        transport = httpx.HTTPTransport(uds=self.socket_path)
        try:
            with httpx.Client(
                transport=transport,
                base_url="http://collector",
                timeout=self.timeout,
            ) as client:
                response = client.get(path, params=params)
        except httpx.HTTPError as error:
            raise CollectorUnavailable(
                f"collector connection failed: {error}"
            ) from error

        try:
            payload = response.json()
        except ValueError as error:
            raise CollectorUnavailable("collector returned invalid JSON") from error
        if response.is_error:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                message = detail.get("collector_error") or detail.get("status")
            else:
                message = detail
            raise CollectorUnavailable(str(message or "collector request failed"))
        if not isinstance(payload, dict):
            raise CollectorUnavailable("collector returned an invalid response")
        return payload

"""Minimal Thanos / Prometheus HTTP API client (standard library only)."""

import json
import ssl
import urllib.parse
import urllib.request
from datetime import datetime


class ThanosError(RuntimeError):
    pass


class ThanosClient:
    def __init__(self, url, token="", ca_bundle="", verify_tls=True, timeout=120):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        if not verify_tls:
            self.ssl_context = ssl._create_unverified_context()
        elif ca_bundle:
            self.ssl_context = ssl.create_default_context(cafile=ca_bundle)
        else:
            self.ssl_context = ssl.create_default_context()

    @classmethod
    def from_settings(cls):
        from django.conf import settings

        return cls(
            settings.THANOS_URL,
            token=settings.THANOS_TOKEN,
            ca_bundle=settings.THANOS_CA_BUNDLE,
            verify_tls=settings.THANOS_VERIFY_TLS,
            timeout=settings.THANOS_TIMEOUT_SECONDS,
        )

    def query(self, promql: str, at: datetime):
        """Instant query. Returns a list of (labels, value) tuples."""
        body = urllib.parse.urlencode(
            {"query": promql, "time": at.timestamp(), "dedup": "true", "partial_response": "false"}
        ).encode()
        req = urllib.request.Request(f"{self.url}/api/v1/query", data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise ThanosError(f"HTTP {e.code} for query {promql!r}: {detail}") from e
        except urllib.error.URLError as e:
            raise ThanosError(f"Cannot reach {self.url}: {e.reason}") from e

        if payload.get("status") != "success":
            raise ThanosError(f"Query failed {promql!r}: {payload.get('error')}")
        data = payload["data"]
        if data["resultType"] == "scalar":
            return [({}, float(data["result"][1]))]
        return [(r["metric"], float(r["value"][1])) for r in data["result"]]

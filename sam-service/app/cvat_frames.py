"""Fetch job frames from CVAT on behalf of the calling user.

The browser plugin calls this service through CVAT's own address (Traefik route /sam2), so the user's CVAT
session cookie arrives with the request. We forward ONLY `Cookie` and `Authorization` to ONE fixed CVAT
address (CVAT_INTERNAL_URL). CVAT then enforces its normal permissions: a caller that cannot read the job
cannot get its frames, and an unauthenticated caller is rejected before any GPU work happens."""
from __future__ import annotations

from typing import Mapping

import logging

import requests

log = logging.getLogger("sam2service.cvat")


class CvatError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


FORWARDED_HEADERS = {"cookie": "Cookie", "authorization": "Authorization"}  # lower-case -> canonical name


class CvatClient:
    def __init__(self, base_url: str, timeout: float = 30.0, host_header: str = "localhost") -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        # CVAT is Django: it answers HTTP 400 to a Host header that is not a valid RFC 1034/1035 domain name,
        # even with ALLOWED_HOSTS='*'. Docker container names such as `cvat_server` contain an underscore, which
        # is invalid, so we reach CVAT by that name but present a valid Host header.
        self.host_header = host_header

    def _headers(self, incoming: Mapping[str, str]) -> dict[str, str]:
        lowered = {k.lower(): v for k, v in incoming.items()}
        headers = {canon: lowered[low] for low, canon in FORWARDED_HEADERS.items() if low in lowered}
        if self.host_header:
            headers["Host"] = self.host_header
        return headers

    def _get(self, path: str, incoming: Mapping[str, str], params: dict | None = None) -> requests.Response:
        try:
            r = requests.get(self.base + path, headers=self._headers(incoming), params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise CvatError(502, f"Cannot reach CVAT at {self.base}: {e}")
        if r.status_code in (401, 403):
            raise CvatError(401 if r.status_code == 401 else 403, "CVAT refused the request: not logged in or no access to this job")
        if r.status_code == 404:
            raise CvatError(404, "CVAT: job or frame not found")
        if r.status_code != 200:
            snippet = r.text[:160].replace("\n", " ")
            log.warning("CVAT answered HTTP %s for %s (%s): %s", r.status_code, path, self.base, snippet)
            raise CvatError(502, f"CVAT answered HTTP {r.status_code} for {path} at {self.base}: {snippet}")
        return r

    def check_auth(self, incoming: Mapping[str, str]) -> None:
        self._get("/api/users/self", incoming)

    def job_range(self, incoming: Mapping[str, str], job_id: int, org: str) -> tuple[int, int]:
        """(start_frame, stop_frame) of the job; also proves the caller may read it."""
        d = self._get(f"/api/jobs/{job_id}", incoming, {"org": org} if org else None).json()
        return int(d["start_frame"]), int(d["stop_frame"])

    def fetch_frame(self, incoming: Mapping[str, str], job_id: int, number: int, org: str) -> bytes:
        params = {"type": "frame", "number": number, "quality": "original"}
        if org:
            params["org"] = org
        return self._get(f"/api/jobs/{job_id}/data", incoming, params).content

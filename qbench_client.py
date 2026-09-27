"""Thin QBench API client used by the Master Spreadsheet app."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Sequence

import jwt  # type: ignore
import requests
import requests.exceptions
from requests.adapters import HTTPAdapter

import qbench_secrets

# Credentials are resolved when a client is constructed, never at import: the
# app must boot (and pass the updater's health check) with no credential store,
# and a pair saved from Settings must take effect without a restart.
DEFAULT_TOKEN_URL = "https://asaplabs.qbench.net/qbench/oauth2/v1/token"
API_BASE_URL = "https://asaplabs.qbench.net/qbench/api/v2"
DEFAULT_TIMEOUT = int(os.getenv("QBENCH_TIMEOUT_SECONDS", "30"))
MAX_CALLS_PER_MINUTE = int(os.getenv("QBENCH_MAX_CALLS_PER_MIN", "340"))

logger = logging.getLogger(__name__)


class QBenchAPIError(RuntimeError):
    """Raised when an API call returns an unexpected response."""


class RateLimiter:
    """Simple thread-safe rate limiter (max calls within a rolling window)."""

    def __init__(self, max_calls: int, period_seconds: float) -> None:
        self.max_calls = max_calls
        self.period = period_seconds
        self.calls = deque()  # timestamps of recent calls
        self.lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a call is available."""
        while True:
            with self.lock:
                now = time.monotonic()
                # Drop expired timestamps
                while self.calls and now - self.calls[0] > self.period:
                    self.calls.popleft()
                if len(self.calls) < self.max_calls:
                    self.calls.append(now)
                    return
                wait_seconds = self.period - (now - self.calls[0]) + 0.01
            time.sleep(max(0.01, min(wait_seconds, 0.25)))


GLOBAL_RATE_LIMITER = RateLimiter(MAX_CALLS_PER_MINUTE, 60.0)


class QBenchAPIClient:
    """Helper for interacting with the QBench API."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        token_url: Optional[str] = None,
        api_base_url: str = API_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
        max_calls_per_minute: int = MAX_CALLS_PER_MINUTE,
    ) -> None:
        self.client_id = client_id or qbench_secrets.get_client_id()
        self.client_secret = client_secret or qbench_secrets.get_client_secret()
        self.token_url = token_url or os.getenv("QBENCH_TOKEN_URL") or DEFAULT_TOKEN_URL
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = timeout
        self._access_token: Optional[str] = None
        self._token_expires_at: int = 0
        self.api_calls: int = 0
        self._token_lock = threading.Lock()
        self.time_offset: float = 0.0

        adapter = HTTPAdapter(pool_connections=12, pool_maxsize=32)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Allow short bursts while respecting the documented caps (shared across instances).
        if max_calls_per_minute == MAX_CALLS_PER_MINUTE:
            self.rate_limiter = GLOBAL_RATE_LIMITER
        else:
            self.rate_limiter = RateLimiter(max_calls_per_minute, 60.0)

    # Authentication -----------------------------------------------------
    def _generate_jwt(self) -> str:
        now = int(time.time() + self.time_offset)
        claims = {"sub": self.client_id, "iat": now, "exp": now + 3600}
        token = jwt.encode(claims, self.client_secret, algorithm="HS256")
        if isinstance(token, bytes):
            token = token.decode("utf-8")
        return token

    def get_access_token(self, *, force: bool = False) -> str:
        now = int(time.time() + self.time_offset)
        with self._token_lock:
            if not force and self._access_token and now < (self._token_expires_at - 15):
                return self._access_token

            data = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._generate_jwt(),
            }
            self.rate_limiter.acquire()
            
            try:
                response = self.session.post(self.token_url, data=data, timeout=self.timeout)
                self.api_calls += 1
                response.raise_for_status()
            except requests.exceptions.HTTPError as e:
                # Check for clock skew (400 Bad Request often means "Future IAT")
                if e.response is not None and e.response.status_code == 400:
                    server_date = e.response.headers.get("Date")
                    if server_date:
                        try:
                            server_dt = parsedate_to_datetime(server_date)
                            server_ts = server_dt.timestamp()
                            local_ts = time.time()
                            # Set offset so local matches server, minus 10s buffer
                            self.time_offset = (server_ts - local_ts) - 10
                            logger.warning(
                                "Detected clock skew. Adjusting time offset by %.2fs", 
                                self.time_offset
                            )
                            
                            # Retry with new token
                            data["assertion"] = self._generate_jwt()
                            self.rate_limiter.acquire()
                            response = self.session.post(self.token_url, data=data, timeout=self.timeout)
                            self.api_calls += 1
                            response.raise_for_status()
                        except Exception:
                            # If retry fails or parsing fails, raise original error
                            raise e
                    else:
                        raise e
                else:
                    raise e

            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise QBenchAPIError(f"Token response missing access_token: {payload}")
            self._access_token = token
            self._token_expires_at = int(time.time() + self.time_offset) + int(payload.get("expires_in", 3600))
            return token

    def _auth_headers(self, *, force: bool = False) -> Dict[str, str]:
        token = self.get_access_token(force=force)
        return {"Authorization": f"Bearer {token}"}

    # Core request helper ------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        params_list: Optional[Sequence[tuple[str, Any]]] = None,
        json_body: Optional[Any] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> requests.Response:
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        headers = self._auth_headers()
        if extra_headers:
            headers.update(extra_headers)
        query_params = params_list if params_list is not None else params

        max_retries = 5
        attempt = 0
        force_refresh = False

        while True:
            self.rate_limiter.acquire()
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=query_params,
                    json=json_body,
                    timeout=timeout or self.timeout,
                )
            except requests.exceptions.ReadTimeout:
                if attempt < max_retries:
                    wait_seconds = min(10, 2 ** attempt)
                    logger.warning(
                        "QBench request timed out at %s. Retrying in %s seconds (attempt %s/%s).",
                        path,
                        wait_seconds,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(wait_seconds)
                    attempt += 1
                    continue
                raise
            self.api_calls += 1

            if response.status_code == 401 and not force_refresh:
                headers = self._auth_headers(force=True)
                force_refresh = True
                attempt += 1
                continue

            if response.status_code == 429 and attempt < max_retries:
                wait_seconds = min(5, 1 + attempt)
                logger.warning(
                    "QBench rate limit hit at %s. Retrying in %s seconds.",
                    path,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                attempt += 1
                continue

            if response.status_code >= 400:
                logger.error(
                    "QBench API error %s at %s: %s",
                    response.status_code,
                    path,
                    response.text,
                )
                raise QBenchAPIError(
                    f"{response.status_code} error calling {path}: {response.text}"
                )
            return response

    # Domain helpers -----------------------------------------------------
    def fetch_tests_by_date_range(
        self,
        start_date: date,
        end_date: Optional[date] = None,
        *,
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        page_size = min(max(page_size, 1), 50)
        if end_date is None:
            end_date = start_date
        params: Dict[str, Any] = {
            "page_size": page_size,
            "start_date_start": start_date.strftime("%m/%d/%Y"),
            "start_date_end": end_date.strftime("%m/%d/%Y"),
        }
        page = 1
        results: List[Dict[str, Any]] = []
        while True:
            params["page_num"] = page
            payload = self.request("GET", "/tests", params=params).json()
            data = payload.get("data", [])
            results.extend(data)
            total_pages = int(payload.get("total_pages", 1))
            if page >= total_pages:
                break
            page += 1
        return results

    def fetch_tests_by_start_date(
        self, start_date: date, *, page_size: int = 50
    ) -> List[Dict[str, Any]]:
        return self.fetch_tests_by_date_range(start_date, start_date, page_size=page_size)

    def fetch_sample(self, sample_id: int) -> Dict[str, Any]:
        payload = self.request("GET", f"/samples/{sample_id}").json()
        return payload.get("data", payload)

    def fetch_samples_by_lab_id(
        self,
        lab_id: str,
        *,
        include_deleted: bool = False,
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        if not lab_id:
            return []
        page_size = min(max(page_size, 1), 50)
        params: Dict[str, Any] = {
            "lab_id": lab_id,
            "page_size": page_size,
            "page_num": 1,
        }
        if include_deleted:
            params["include_deleted"] = "TRUE"
        results: List[Dict[str, Any]] = []
        while True:
            payload = self.request("GET", "/samples", params=params).json()
            data = payload.get("data", [])
            results.extend(data)
            total_pages = int(payload.get("total_pages", 1) or 1)
            if params["page_num"] >= total_pages:
                break
            params["page_num"] += 1
        return results

    def fetch_tests_for_sample_ids(
        self,
        sample_ids: Sequence[int],
        *,
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        normalized_ids: List[int] = []
        for sample_id in sample_ids:
            try:
                normalized_ids.append(int(sample_id))
            except (TypeError, ValueError):
                continue
        if not normalized_ids:
            return []
        page_size = min(max(page_size, 1), 50)
        page = 1
        results: List[Dict[str, Any]] = []
        while True:
            params_list: List[tuple[str, Any]] = [
                ("page_num", page),
                ("page_size", page_size),
            ]
            for sample_id in normalized_ids:
                params_list.append(("sample_ids", sample_id))
            payload = self.request("GET", "/tests", params_list=params_list).json()
            data = payload.get("data", [])
            results.extend(data)
            total_pages = int(payload.get("total_pages", 1) or 1)
            if page >= total_pages:
                break
            page += 1
        return results

    def fetch_order(self, order_id: int) -> Dict[str, Any]:
        """Fetch a single order record."""
        payload = self.request("GET", f"/orders/{order_id}").json()
        return payload.get("data", payload)

    def fetch_tests_by_order_id(
        self,
        order_id: int,
        *,
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return all tests belonging to a single order (one API call per page)."""
        page_size = min(max(page_size, 1), 50)
        page = 1
        results: List[Dict[str, Any]] = []
        while True:
            params: Dict[str, Any] = {"page_num": page, "page_size": page_size}
            payload = self.request("GET", f"/orders/{order_id}/tests", params=params).json()
            data = payload.get("data", [])
            results.extend(data)
            total_pages = int(payload.get("total_pages", 1) or 1)
            if page >= total_pages:
                break
            page += 1
        return results

    def update_test_result(self, test_id: int, value: str) -> Dict[str, Any]:
        body = [{"id": test_id, "results": value}]
        return self.request("PATCH", "/tests", json_body=body).json()

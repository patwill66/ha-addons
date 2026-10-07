"""Thin client for the official Mammotion REST API (read-only endpoints only).

Every response is HTTP 200 with a {code, msg, data, requestId} envelope; code 0 means success
and code 401 means the token was rejected (expired or revoked by a newer login).
"""

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://id.mammotion.com/oauth2/token"
API_BASE = "https://api-open.mammotion.com"
USER_AGENT = "mammotion-luba-collector/0.1"


class ApiError(Exception):
    """The API answered with a non-zero envelope code."""

    def __init__(self, code, msg, request_id=None):
        super().__init__(f"API code {code}: {msg}")
        self.code, self.msg, self.request_id = code, msg, request_id


class AuthError(ApiError):
    """Token rejected (code 401) or authentication refused."""


class TransientError(Exception):
    """Network failure, timeout, 5xx or unparseable response — worth retrying later."""


def http_request(method: str, url: str, headers: dict, body: bytes | None, timeout: int = 30):
    """Default transport. Returns (http_status, text); raises TransientError on network failure."""
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **headers}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise TransientError(f"network error: {getattr(e, 'reason', e)}") from None


def parse_envelope(status: int, text: str):
    """Return (data, request_id) or raise ApiError / AuthError / TransientError."""
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        raise TransientError(f"HTTP {status}: non-JSON response") from None
    if status >= 500:
        raise TransientError(f"HTTP {status}")
    if not isinstance(body, dict) or "code" not in body:
        raise ApiError(None, f"unexpected response shape (HTTP {status})")
    code, rid = body.get("code"), body.get("requestId")
    if code == 0:
        return body.get("data"), rid
    if code == 401 or status == 401:
        raise AuthError(code, body.get("msg"), rid)
    raise ApiError(code, body.get("msg"), rid)


class MammotionClient:
    def __init__(self, tokens, transport=http_request, base_url: str = API_BASE):
        self.tokens = tokens
        self.transport = transport
        self.base_url = base_url

    def _call(self, method: str, path: str, json_body=None):
        body = json.dumps(json_body).encode() if json_body is not None else None
        for attempt in (1, 2):
            headers = {"Accept": "application/json", "Authorization": f"Bearer {self.tokens.token()}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            status, text = self.transport(method, f"{self.base_url}{path}", headers, body)
            try:
                return parse_envelope(status, text)
            except AuthError:
                if attempt == 2:
                    raise
                self.tokens.invalidate("API rejected the token (401)")  # may raise if throttled
        raise AssertionError("unreachable")

    @staticmethod
    def _q(device_id: str) -> str:
        return urllib.parse.quote(device_id, safe="")

    def list_mowers(self) -> list:
        data, _ = self._call("GET", "/v1/mowers")
        return data or []

    def device_detail(self, device_id: str):
        return self._call("GET", f"/v1/mower/{self._q(device_id)}")

    def plan(self, device_id: str) -> list:
        data, _ = self._call("GET", f"/v1/mower/{self._q(device_id)}/plan")
        return data or []

    def work_params(self, device_id: str) -> dict:
        """NOTE: asks the mower to report its parameters (a command, not a pure read). Use sparingly."""
        data, _ = self._call("GET", f"/v1/mower/{self._q(device_id)}/work-params")
        return data or {}

    def error_codes(self, device_id: str, page: int, page_size: int = 50) -> dict:
        data, _ = self._call("POST", "/v1/mower/error-codes/search",
                             {"deviceId": device_id, "pageNumber": page, "pageSize": page_size})
        return data or {}

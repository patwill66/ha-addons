"""Read-only client for the eero cloud API (https://api-user.e2ro.com/2.2/...).

The API is private and undocumented; docs/API_NOTES.md records what each endpoint returns.

Guarantees:
- Only GET requests, plus POSTs to the three login endpoints. Anything else raises Refused before
  any network I/O. Nothing in this package can change an eero network.
- The session token is sent only as the `s` cookie to API_HOST, never logged, and stored only
  through the SessionStore given to the client.
- A 401 "session refresh" is answered with at most one POST /2.2/login/refresh per request; if that
  doesn't restore access, AuthRequired is raised and the caller must ask the user to log in again.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API_HOST = "https://api-user.e2ro.com"
LOGIN, VERIFY, REFRESH = "/2.2/login", "/2.2/login/verify", "/2.2/login/refresh"
ALLOWED_POSTS = {LOGIN, VERIFY, REFRESH}
USER_AGENT = "eero-network-collector (read-only; personal Home Assistant)"
SESSION_ERRORS = {"error.session.refresh", "error.session.invalid", "error.session.expired"}


class Refused(Exception):
    """A request this client must never make."""


class EeroError(Exception):
    def __init__(self, message, status=None, error=None):
        super().__init__(message)
        self.status, self.error = status, error


class AuthRequired(EeroError):
    """No session, or the session can't be refreshed: the user must log in again."""


class RateLimited(EeroError):
    def __init__(self, message, retry_after=None):
        super().__init__(message, 429, "rate_limited")
        self.retry_after = retry_after


class PremiumRequired(EeroError):
    """403 error.premium.*: the endpoint needs an Eero Plus subscription."""


def path_template(path):
    """/2.2/networks/123/devices/aabbccddeeff -> /2.2/networks/{id}/devices/{id} (for logs and stats)."""
    path = path.split("?", 1)[0]
    return "/".join("{id}" if re.fullmatch(r"\d+|[0-9a-f]{12}|[0-9a-f:]{17}", p or "-") else p
                    for p in path.split("/"))


class EeroClient:
    def __init__(self, session_store, timeout=30, on_call=None, opener=None, min_gap=1.0, sleep=time.sleep):
        self.store = session_store
        self.timeout = timeout
        self.min_gap, self.sleep, self._last = min_gap, sleep, 0.0  # never more than one request per min_gap s
        self.on_call = on_call or (lambda **_: None)
        self.opener = opener or urllib.request.urlopen

    # --- public API -------------------------------------------------------------------------
    def get(self, path, params=None):
        """GET a path (absolute "/2.2/..." as the API's own links are) and return its `data`."""
        token = self.store.load()
        if not token:
            raise AuthRequired("not logged in")
        return self._call("GET", path, params=params, token=token, allow_refresh=True)

    def login(self, identifier):
        """Starts a login; eero sends a code by email or SMS. Returns the pending token."""
        data = self._call("POST", LOGIN, body={"login": identifier}, token=None, allow_refresh=False)
        token = (data or {}).get("user_token")
        if not token:
            raise EeroError("login response carried no token")
        return token

    def verify(self, pending_token, code):
        """Completes a login with the code; stores the now-verified token. Returns the account name."""
        data = self._call("POST", VERIFY, body={"code": code}, token=pending_token, allow_refresh=False)
        self.store.save(pending_token)
        return (data or {}).get("name")

    # --- transport --------------------------------------------------------------------------
    def _call(self, method, path, params=None, body=None, token=None, allow_refresh=False):
        status, payload = self._request(method, path, params, body, token)
        if status == 401 and allow_refresh and _error(payload) in SESSION_ERRORS:
            token = self._refresh(token)
            status, payload = self._request(method, path, params, body, token)
            if status == 401:
                raise AuthRequired("session refresh did not restore access", 401, _error(payload))
        return _unwrap(status, payload, path)

    def _refresh(self, token):
        status, payload = self._request("POST", REFRESH, None, None, token)
        new = ((payload or {}).get("data") or {}).get("user_token") if status == 200 else None
        if not new:
            raise AuthRequired("session expired; log in again", status, _error(payload))
        self.store.save(new)
        return new

    def _request(self, method, path, params, body, token):
        if not path.startswith("/2.2/") or ".." in path or "://" in path:
            raise Refused(f"not an API path: {path!r}")
        if method != "GET" and not (method == "POST" and path in ALLOWED_POSTS):
            raise Refused(f"{method} {path_template(path)} is not allowed (read-only client)")
        url = API_HOST + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if token:
            headers["Cookie"] = f"s={token}"
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        wait = self._last + self.min_gap - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last = started = time.monotonic()
        raw, status, retry_after = b"", None, None
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as err:
            raw, status = err.read(), err.code
            retry_after = err.headers.get("Retry-After") if err.headers else None
            err.close()
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            self.on_call(method=method, path=path_template(path), status=0,
                         ms=int((time.monotonic() - started) * 1000), size=0)
            raise EeroError(f"network error: {getattr(err, 'reason', err)}") from None
        self.on_call(method=method, path=path_template(path), status=status,
                     ms=int((time.monotonic() - started) * 1000), size=len(raw))
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = None
        if status == 429:
            try:
                retry_after = float(retry_after) if retry_after else None
            except ValueError:
                retry_after = None
            raise RateLimited("rate limited by eero", retry_after)
        return status, payload


def _error(payload):
    return ((payload or {}).get("meta") or {}).get("error") if isinstance(payload, dict) else None


def _unwrap(status, payload, path):
    if status == 200 or status == 201:
        return (payload or {}).get("data") if isinstance(payload, dict) else None
    err = _error(payload)
    where = path_template(path)
    if status == 401:
        raise AuthRequired(f"{where}: not authorized ({err})", status, err)
    if status == 403 and err and err.startswith("error.premium"):
        raise PremiumRequired(f"{where}: needs Eero Plus ({err})", status, err)
    raise EeroError(f"{where}: HTTP {status} ({err})", status, err)

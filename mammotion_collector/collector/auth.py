"""In-process OAuth token manager: authenticate once, reuse the token, renew only when needed.

A Mammotion client may hold at most 2 live tokens; a 3rd login revokes the oldest. So this
process logs in once and reuses that token (about 15 days) until shortly before it expires.
The refresh_token grant is documented on the portal but has not been verified, so it is not
used; we simply log in again near expiry. Tokens are kept in memory only, never persisted.
"""

import threading
import time
import urllib.parse

from .api import TOKEN_URL, ApiError, AuthError, http_request, parse_envelope


class TokenManager:
    def __init__(self, client_id: str, client_secret: str, transport=http_request, clock=time.time,
                 min_reauth_seconds: int = 300, on_auth=None):
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport
        self._clock = clock
        self._min_reauth_seconds = min_reauth_seconds
        self._on_auth = on_auth  # callback(reason, expires_in) for logging; never receives the token
        self._lock = threading.Lock()
        self._token = None
        self._expires_at = 0.0
        self._renew_at = 0.0
        self._last_auth_at = None
        self.auth_count = 0

    def token(self) -> str:
        with self._lock:  # one authentication at a time for the whole process
            now = self._clock()
            if self._token is None:
                self._authenticate("startup" if self.auth_count == 0 else "token invalidated")
            elif now >= self._renew_at:
                self._authenticate("token near expiry")
            return self._token

    def invalidate(self, reason: str) -> None:
        """Drop the current token after a 401. Refuses to loop: at most one re-login per window."""
        with self._lock:
            if self._last_auth_at is not None and self._clock() - self._last_auth_at < self._min_reauth_seconds:
                raise AuthError(401, f"{reason}; last login was under {self._min_reauth_seconds}s ago — "
                                     "not logging in again yet (is another process using this client?)")
            self._token = None

    def _authenticate(self, reason: str) -> None:
        body = urllib.parse.urlencode({"client_id": self._client_id, "client_secret": self._client_secret,
                                       "grant_type": "client_credentials"}).encode()
        status, text = self._transport("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded",
                                                           "Accept": "application/json"}, body)
        try:
            data, _ = parse_envelope(status, text)
        except AuthError:
            raise
        except ApiError as e:  # e.g. 40103 bad client id/secret, 40109 grant type not allowed
            raise AuthError(e.code, f"login refused: {e.msg}") from None
        if not isinstance(data, dict) or not data.get("access_token"):
            raise AuthError(None, "token response had no access_token")
        expires_in = int(data.get("expires_in") or 3600)
        now = self._clock()
        self._token = data["access_token"]
        self._expires_at = now + expires_in
        # Renew a little early: 10% of the lifetime, capped at one day.
        self._renew_at = self._expires_at - min(86400, expires_in * 0.1)
        self._last_auth_at = now
        self.auth_count += 1
        if self._on_auth:
            self._on_auth(reason, expires_in)

    @property
    def expires_at(self) -> float:
        return self._expires_at

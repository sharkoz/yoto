from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import httpx

from .errors import YotoError, require
from .storage import ConfigStore, KeychainTokenStore, TokenStore

DEFAULT_AUTH_BASE = "https://login.yotoplay.com"
DEFAULT_AUDIENCE = "https://api.yotoplay.com"
LOOPBACK_CALLBACK_URL = "http://127.0.0.1:8787/callback"
DEFAULT_REMOTE_CALLBACK_URL = "https://yiochen.github.io/yoto-auth-callback/"
PENDING_AUTH_TTL_SECONDS = 600
# Request inherited capabilities explicitly. Yoto documents the manage scopes as
# including them, but live endpoints can require them to appear in the token scope.
SCOPES = (
    "family:library:view user:content:view user:content:manage "
    "user:icons:manage offline_access"
)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def authorization_url(
    client_id: str,
    challenge: str,
    state: str,
    auth_base: str = DEFAULT_AUTH_BASE,
    redirect_uri: str = LOOPBACK_CALLBACK_URL,
) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "audience": DEFAULT_AUDIENCE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{auth_base.rstrip('/')}/authorize?{query}"


class AuthManager:
    def __init__(
        self,
        config: ConfigStore | None = None,
        tokens: TokenStore | None = None,
        http: httpx.Client | None = None,
        auth_base: str = DEFAULT_AUTH_BASE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or ConfigStore()
        self.tokens = tokens or KeychainTokenStore()
        self.http = http or httpx.Client(timeout=30)
        self.auth_base = auth_base
        self.clock = clock

    def client_id(self) -> str:
        client_id = self.config.read().get("client_id")
        require(bool(client_id), "not_configured", "Run `auth configure --client-id …` first")
        return str(client_id)

    def configure(self, client_id: str) -> dict[str, str]:
        require(bool(client_id.strip()), "invalid_client_id", "Client ID cannot be empty")
        self.config.write({"client_id": client_id.strip()})
        return {"client_id": client_id.strip(), "config_path": str(self.config.path)}

    def _pending_account(self, client_id: str) -> str:
        return f"{client_id}:pending-login"

    @staticmethod
    def _validate_remote_callback(callback_url: str) -> str:
        callback_url = callback_url.strip()
        parsed = urllib.parse.urlparse(callback_url)
        require(
            parsed.scheme == "https" and bool(parsed.netloc) and not parsed.fragment,
            "invalid_callback_url",
            "The phone callback must be an HTTPS URL without a fragment",
        )
        return callback_url

    def begin(self, callback_url: str = DEFAULT_REMOTE_CALLBACK_URL) -> dict[str, Any]:
        client_id = self.client_id()
        redirect_uri = self._validate_remote_callback(callback_url)
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        expires_at = self.clock() + PENDING_AUTH_TTL_SECONDS
        self.tokens.save(
            self._pending_account(client_id),
            {
                "verifier": verifier,
                "state": state,
                "redirect_uri": redirect_uri,
                "expires_at": expires_at,
            },
        )
        return {
            "authorization_url": authorization_url(
                client_id,
                challenge,
                state,
                self.auth_base,
                redirect_uri,
            ),
            "callback_url": redirect_uri,
            "expires_at": expires_at,
        }

    def complete(self, result: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(result, str):
            try:
                parsed_result = json.loads(result)
            except json.JSONDecodeError as exc:
                raise YotoError(
                    "invalid_auth_result",
                    "The pasted authorization result is not valid JSON",
                ) from exc
        else:
            parsed_result = result
        require(
            isinstance(parsed_result, dict),
            "invalid_auth_result",
            "The pasted authorization result must be a JSON object",
        )
        code = parsed_result.get("code")
        returned_state = parsed_result.get("state")
        require(
            isinstance(code, str) and bool(code),
            "invalid_auth_result",
            "The pasted authorization result has no code",
        )
        require(
            isinstance(returned_state, str) and bool(returned_state),
            "invalid_auth_result",
            "The pasted authorization result has no state",
        )

        client_id = self.client_id()
        pending_account = self._pending_account(client_id)
        pending = self.tokens.load(pending_account)
        require(
            bool(pending),
            "auth_not_started",
            "No pending phone authorization was found; run `auth begin` first",
        )
        if float(pending.get("expires_at", 0)) <= self.clock():
            self.tokens.delete(pending_account)
            raise YotoError(
                "auth_result_expired",
                "The pending phone authorization expired; run `auth begin` again",
            )
        expected_state = str(pending.get("state", ""))
        require(
            bool(expected_state) and secrets.compare_digest(returned_state, expected_state),
            "oauth_state_mismatch",
            "The authorization result does not belong to this login attempt",
        )
        verifier = str(pending.get("verifier", ""))
        redirect_uri = str(pending.get("redirect_uri", ""))
        require(
            bool(verifier and redirect_uri),
            "pending_auth_invalid",
            "The pending authorization data is incomplete; run `auth begin` again",
        )
        token = self.exchange_code(code, verifier, redirect_uri)
        self.tokens.delete(pending_account)
        return {"authenticated": True, "expires_at": token["expires_at"]}

    def _save_token_response(self, client_id: str, response: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
        token = dict(response)
        if previous and not token.get("refresh_token"):
            token["refresh_token"] = previous.get("refresh_token")
        token["expires_at"] = self.clock() + float(token.get("expires_in", 3600))
        require(bool(token.get("access_token")), "oauth_invalid_response", "OAuth response has no access token")
        self.tokens.save(client_id, token)
        return token

    def exchange_code(
        self,
        code: str,
        verifier: str,
        redirect_uri: str = LOOPBACK_CALLBACK_URL,
    ) -> dict[str, Any]:
        client_id = self.client_id()
        response = self._token_post(
            f"{self.auth_base.rstrip('/')}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
        )
        self._raise_oauth(response)
        return self._save_token_response(client_id, response.json())

    def login(self, timeout: int = 180) -> dict[str, Any]:
        client_id = self.client_id()
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        result: dict[str, str] = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(handler_self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(handler_self.path)
                if parsed.path != "/callback":
                    handler_self.send_error(404)
                    return
                values = urllib.parse.parse_qs(parsed.query)
                result["code"] = values.get("code", [""])[0]
                result["state"] = values.get("state", [""])[0]
                result["error"] = values.get("error_description", values.get("error", [""]))[0]
                body = b"Yoto authorization received. You can close this window."
                handler_self.send_response(200)
                handler_self.send_header("Content-Type", "text/plain; charset=utf-8")
                handler_self.send_header("Content-Length", str(len(body)))
                handler_self.end_headers()
                handler_self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                return

        url = authorization_url(
            client_id,
            challenge,
            state,
            self.auth_base,
            LOOPBACK_CALLBACK_URL,
        )
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 8787), CallbackHandler)
        except OSError as exc:
            raise YotoError(
                "callback_unavailable",
                "Could not listen on 127.0.0.1:8787; close the process using that port and retry",
                str(exc),
            ) from exc
        server.timeout = timeout
        print(f"Open this URL to authorize Yoto:\n{url}", file=sys.stderr)
        webbrowser.open(url)
        server.handle_request()
        server.server_close()
        if result.get("error"):
            raise YotoError("oauth_denied", result["error"])
        require(bool(result.get("code")), "oauth_timeout", "Authorization did not complete before the timeout")
        require(result.get("state") == state, "oauth_state_mismatch", "OAuth callback state did not match")
        token = self.exchange_code(result["code"], verifier, LOOPBACK_CALLBACK_URL)
        return {"authenticated": True, "expires_at": token["expires_at"]}

    def status(self) -> dict[str, Any]:
        client_id = self.client_id()
        token = self.tokens.load(client_id)
        granted_scopes = sorted(str(token.get("scope", "")).split()) if token else []
        missing_scopes = sorted(set(SCOPES.split()) - set(granted_scopes))
        return {
            "configured": True,
            "authenticated": bool(token and token.get("access_token")),
            "expires_at": token.get("expires_at") if token else None,
            "refreshable": bool(token and token.get("refresh_token")),
            "scopes": granted_scopes,
            "missing_scopes": missing_scopes,
        }

    def logout(self) -> dict[str, bool]:
        client_id = self.client_id()
        self.tokens.delete(client_id)
        self.tokens.delete(self._pending_account(client_id))
        return {"authenticated": False}

    def access_token(self, force_refresh: bool = False) -> str:
        client_id = self.client_id()
        token = self.tokens.load(client_id)
        require(bool(token), "not_authenticated", "Run `auth login` first")
        expires_at = float(token.get("expires_at", 0))
        if force_refresh or expires_at <= self.clock() + 60:
            refresh = token.get("refresh_token")
            require(bool(refresh), "token_expired", "The Yoto session expired; run `auth login` again")
            response = self._token_post(
                f"{self.auth_base.rstrip('/')}/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh,
                },
            )
            self._raise_oauth(response)
            token = self._save_token_response(client_id, response.json(), token)
        return str(token["access_token"])

    def _token_post(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.http.post(url, **kwargs)
        except httpx.RequestError as exc:
            raise YotoError(
                "network_error",
                "Could not reach Yoto's authorization service",
                {"exception": type(exc).__name__},
            ) from exc

    @staticmethod
    def _raise_oauth(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            details = response.json()
        except (ValueError, json.JSONDecodeError):
            details = response.text
        raise YotoError("oauth_error", "Yoto authorization failed", details)

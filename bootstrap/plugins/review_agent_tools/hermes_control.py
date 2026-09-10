"""Fixed, bounded client for Hermes-owned provider authentication state."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from . import review_contract
from .model_accounts import (
    AccountAvailability,
    ManagedRuntimeStatus,
    ModelProvider,
    RuntimeAccount,
)
from .model_quota import (
    AccountQuota,
    ManagedQuota,
    ObservedQuota,
    QuotaBucket,
    QuotaSnapshot,
    QuotaWindow,
)


MAX_RESPONSE_BYTES = 256 * 1024
MANAGED_REVIEW_PATH = "/v1/review-agent/review"
TIMEOUT_SECONDS = 20.0
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{22,80}$")
_PROVIDERS = {"openai-codex", "anthropic"}


class HermesControlError(RuntimeError):
    """Hermes control returned an unavailable or invalid safe response."""


class HermesControlConfigurationError(HermesControlError):
    """The server-only Hermes control configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider: str
    name: str
    connected: bool
    flow: str
    action: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProviderModel:
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class LoginSession:
    session_id: str
    status: str
    user_code: str | None
    verification_url: str | None
    expires_in: int | None
    poll_interval: int


@dataclass(frozen=True, slots=True)
class Cancellation:
    cancelled: bool
    session_id: str


def _quota_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HermesControlError("Account quota is invalid")
    return cast(dict[str, object], value)


def _quota_number(value: object, *, maximum: float) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise HermesControlError("Account quota number is invalid")
    number = cast(int | float, value)
    if not 0 <= number <= maximum:
        raise HermesControlError("Account quota number is invalid")
    return float(number)


def _quota_integer(value: object, *, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise HermesControlError("Account quota count is invalid")
    return value


def _quota_bool(value: object) -> bool | None:
    if value is not None and type(value) is not bool:
        raise HermesControlError("Account quota state is invalid")
    return value


def _quota_text(value: object) -> str | None:
    return None if value is None else _text(value, field="quota label", maximum=200)


def _managed_quota(payload: dict[str, object], provider: ModelProvider) -> ManagedQuota:
    observed = _quota_object(payload.get("observation"))
    data = _quota_object(observed.get("data"))
    fingerprint = observed.get("identity_sha256")
    if fingerprint is not None and (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
    ):
        raise HermesControlError("Account quota identity is invalid")
    reason = data.get("unavailable_reason")
    if reason not in (
        None,
        "not_supported",
        "account_unavailable",
        "provider_unavailable",
    ):
        raise HermesControlError("Account quota availability is invalid")
    if data.get("provider") != provider.value or any(
        type(data.get(name)) is not bool for name in ("refreshing", "stale")
    ):
        raise HermesControlError("Account quota state is invalid")
    snapshot = None
    if data.get("snapshot") is not None:
        raw = _quota_object(data["snapshot"])
        fetched = _quota_number(raw.get("fetched_at"), maximum=253402300799)
        if fetched is None or fingerprint is None:
            raise HermesControlError("Account quota observation is incomplete")
        rows = raw.get("buckets")
        if not isinstance(rows, list) or len(cast(list[object], rows)) > 32:
            raise HermesControlError("Account quota buckets are invalid")
        buckets: list[QuotaBucket] = []
        for row in cast(list[object], rows):
            bucket = _quota_object(row)
            values = bucket.get("windows")
            if not isinstance(values, list) or len(cast(list[object], values)) > 2:
                raise HermesControlError("Account quota windows are invalid")
            windows: list[QuotaWindow] = []
            for value in cast(list[object], values):
                window = _quota_object(value)
                kind = window.get("kind")
                if kind not in ("primary", "secondary"):
                    raise HermesControlError("Account quota window is invalid")
                windows.append(
                    QuotaWindow(
                        kind,
                        _quota_number(window.get("used_percent"), maximum=2147483647),
                        _quota_integer(
                            window.get("duration_seconds"),
                            minimum=1,
                            maximum=2147483647,
                        ),
                        _quota_number(window.get("resets_at"), maximum=253402300799),
                    )
                )
            if len({window.kind for window in windows}) != len(windows):
                raise HermesControlError("Account quota windows are duplicated")
            buckets.append(
                QuotaBucket(
                    _text(bucket.get("id"), field="quota bucket", maximum=200),
                    _quota_text(bucket.get("name")),
                    _quota_text(bucket.get("normal_model_slug")),
                    _quota_bool(bucket.get("allowed")),
                    _quota_bool(bucket.get("limit_reached")),
                    tuple(windows),
                )
            )
        if len({bucket.id for bucket in buckets}) != len(buckets):
            raise HermesControlError("Account quota buckets are duplicated")
        snapshot = QuotaSnapshot(
            fetched,
            _quota_text(raw.get("plan")),
            tuple(buckets),
            _quota_integer(
                raw.get("reset_credits_available"),
                minimum=0,
                maximum=9223372036854775807,
            ),
            _quota_text(raw.get("limit_reached_type")),
            _quota_bool(raw.get("spend_control_reached")),
        )
    runtime_key, instance = _runtime_identity(payload)
    return ManagedQuota(
        runtime_key,
        instance,
        ObservedQuota(
            fingerprint,
            AccountQuota(
                provider,
                snapshot,
                cast(bool, data["refreshing"]),
                cast(bool, data["stale"]),
                _quota_number(data.get("next_refresh_at"), maximum=253402300799),
                reason,
            ),
        ),
    )


def _runtime_identity(payload: dict[str, object]) -> tuple[str, UUID]:
    runtime_key = payload.get("runtime_key")
    if (
        not isinstance(runtime_key, str)
        or re.fullmatch(r"[a-z][a-z0-9-]{0,62}", runtime_key) is None
    ):
        raise HermesControlError("Managed runtime identity is invalid")
    try:
        return runtime_key, UUID(str(payload.get("instance_id")))
    except ValueError as exc:
        raise HermesControlError("Managed runtime identity is invalid") from exc


def _managed_status(payload: dict[str, object]) -> ManagedRuntimeStatus:
    raw_accounts = payload.get("accounts")
    if not isinstance(raw_accounts, list) or len(
        cast(list[object], raw_accounts)
    ) != len(ModelProvider):
        raise HermesControlError("Managed account status is invalid")
    accounts: list[RuntimeAccount] = []
    try:
        for raw in cast(list[object], raw_accounts):
            if not isinstance(raw, dict):
                raise ValueError("Invalid account")
            row = cast(dict[str, object], raw)
            count = row.get("account_count")
            fingerprint = row.get("identity_sha256")
            if type(count) is not int or not 0 <= count <= 1000:
                raise ValueError("Invalid account count")
            if fingerprint is not None and (
                not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            ):
                raise ValueError("Invalid account identity")
            availability = AccountAvailability(row.get("availability"))
            if (availability is AccountAvailability.AVAILABLE) != (
                count == 1 and fingerprint is not None
            ):
                raise ValueError("Inconsistent account status")
            accounts.append(
                RuntimeAccount(
                    ModelProvider(row.get("provider")), availability, count, fingerprint
                )
            )
        if {account.provider for account in accounts} != set(ModelProvider):
            raise ValueError("Duplicate account")
        runtime_key, instance = _runtime_identity(payload)
        return ManagedRuntimeStatus(
            runtime_key,
            instance,
            review_contract.parse_contract(payload.get("contract")),
            tuple(accounts),
        )
    except (ValueError, TypeError) as exc:
        raise HermesControlError("Managed account status is invalid") from exc


Readiness = Literal["ok", "degraded", "unavailable", "retrying", "unknown"]
CheckName = Literal[
    "state_db",
    "session_store",
    "config",
    "model",
    "disk",
    "gateway",
    "background_queues",
]
_CHECKS: tuple[CheckName, ...] = (
    "state_db",
    "session_store",
    "config",
    "model",
    "disk",
    "gateway",
    "background_queues",
)


@dataclass(frozen=True, slots=True)
class RuntimeCheck:
    name: CheckName
    status: Readiness


@dataclass(frozen=True, slots=True)
class HermesRuntimeStatus:
    status: Readiness
    version: str
    model: str | None
    active_agents: int
    busy: bool
    drainable: bool
    chat_available: bool
    checks: tuple[RuntimeCheck, ...]


def _readiness(value: object) -> Readiness:
    if value not in ("ok", "degraded", "unavailable", "retrying", "unknown"):
        raise HermesControlError("Hermes readiness status is invalid")
    return value


def _runtime_status(payload: dict[str, object]) -> HermesRuntimeStatus:
    active = payload.get("active_agents")
    if type(active) is not int or active < 0:
        raise HermesControlError("Hermes active agent count is invalid")
    for name in ("busy", "drainable", "chat_available"):
        if type(payload.get(name)) is not bool:
            raise HermesControlError("Hermes runtime state is invalid")
    rows = payload.get("checks")
    if not isinstance(rows, list):
        raise HermesControlError("Hermes readiness checks are incomplete")
    values = cast(list[object], rows)
    if len(values) != len(_CHECKS):
        raise HermesControlError("Hermes readiness checks are incomplete")
    checks: list[RuntimeCheck] = []
    for raw in values:
        if not isinstance(raw, dict):
            raise HermesControlError("Hermes readiness check is invalid")
        item = cast(dict[str, object], raw)
        if item.get("name") not in _CHECKS:
            raise HermesControlError("Hermes readiness check is invalid")
        checks.append(
            RuntimeCheck(cast(CheckName, item["name"]), _readiness(item.get("status")))
        )
    if len({check.name for check in checks}) != len(_CHECKS):
        raise HermesControlError("Hermes readiness checks are incomplete")
    model = payload.get("model")
    return HermesRuntimeStatus(
        status=_readiness(payload.get("status")),
        version=_text(payload.get("version"), field="version", maximum=80),
        model=_text(model, field="model", maximum=200) if model is not None else None,
        active_agents=active,
        busy=cast(bool, payload["busy"]),
        drainable=cast(bool, payload["drainable"]),
        chat_available=cast(bool, payload["chat_available"]),
        checks=tuple(checks),
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def validate_session_id(value: str) -> str:
    if not _SESSION_ID.fullmatch(value):
        raise HermesControlError("provider login session id is invalid")
    return value


def _origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HermesControlConfigurationError(
            "Hermes control URL must be one HTTP origin"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise HermesControlError(f"Hermes {field} is invalid")
    result = value.strip()
    if not result or len(result) > maximum or "\x00" in result:
        raise HermesControlError(f"Hermes {field} is invalid")
    return result


def _verification_url(value: object) -> str:
    url = _text(value, field="verification URL", maximum=500)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "auth.openai.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/codex/device"
        or parsed.query
        or parsed.fragment
    ):
        raise HermesControlError("Hermes verification URL is invalid")
    return url


class HermesControlClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.base_url = _origin(base_url)
        self._token = _text(token, field="session token", maximum=4096)
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def _request(self, method: str, path: str) -> dict[str, object]:
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            headers={
                "Accept": "application/json",
                "X-Hermes-Session-Token": self._token,
            },
            data=b"" if method == "POST" else None,
        )
        try:
            with self.opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise HermesControlError("Hermes provider control is unavailable") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise HermesControlError("Hermes provider response is too large")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HermesControlError("Hermes provider response is invalid") from exc
        if not isinstance(payload, dict):
            raise HermesControlError("Hermes provider response is invalid")
        return cast(dict[str, object], payload)

    def provider_statuses(self) -> tuple[ProviderStatus, ...]:
        payload = self._request("GET", "/api/providers/oauth")
        rows = payload.get("providers")
        if not isinstance(rows, list):
            raise HermesControlError("Hermes provider status is invalid")
        status_rows = cast(list[object], rows)
        if len(status_rows) > 100:
            raise HermesControlError("Hermes provider status is invalid")
        found: dict[str, ProviderStatus] = {}
        for raw in status_rows:
            if not isinstance(raw, dict):
                continue
            item = cast(dict[object, object], raw)
            if item.get("id") not in _PROVIDERS:
                continue
            provider = str(item["id"])
            status = item.get("status")
            if not isinstance(status, dict):
                raise HermesControlError("Hermes provider status is invalid")
            status_item = cast(dict[object, object], status)
            if not isinstance(status_item.get("logged_in"), bool):
                raise HermesControlError("Hermes provider status is invalid")
            expires_at = None
            expires = status_item.get("expires_at")
            if expires is not None:
                if not isinstance(expires, str):
                    raise HermesControlError("Hermes provider expiry is invalid")
                try:
                    expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise HermesControlError(
                        "Hermes provider expiry is invalid"
                    ) from exc
            found[provider] = ProviderStatus(
                provider=provider,
                name=(
                    "ChatGPT or Codex subscription"
                    if provider == "openai-codex"
                    else "Anthropic"
                ),
                connected=cast(bool, status_item["logged_in"]),
                flow="device_code" if provider == "openai-codex" else "external_cli",
                action=(
                    "Connect in browser"
                    if provider == "openai-codex"
                    else "hermes auth add anthropic"
                ),
                expires_at=expires_at,
            )
        if set(found) != _PROVIDERS:
            raise HermesControlError("Hermes provider status is incomplete")
        return tuple(found[item] for item in ("openai-codex", "anthropic"))

    def runtime_status(self) -> HermesRuntimeStatus:
        return _runtime_status(self._request("GET", "/api/runtime"))

    def models(self) -> tuple[ProviderModel, ...]:
        payload = self._request("GET", "/api/model/options")
        rows = payload.get("providers")
        if not isinstance(rows, list):
            raise HermesControlError("Hermes model options are invalid")
        model_rows = cast(list[object], rows)
        if len(model_rows) > 100:
            raise HermesControlError("Hermes model options are invalid")
        models: list[ProviderModel] = []
        for row in model_rows:
            if not isinstance(row, dict):
                raise HermesControlError("Hermes model options are invalid")
            item = cast(dict[object, object], row)
            provider = item.get("slug", item.get("id", item.get("provider")))
            if provider not in _PROVIDERS:
                continue
            values = item.get("models")
            if not isinstance(values, list):
                raise HermesControlError("Hermes model options are invalid")
            model_values = cast(list[object], values)
            if len(model_values) > 100:
                raise HermesControlError("Hermes model options are invalid")
            for raw in model_values:
                value = (
                    cast(dict[object, object], raw).get("id")
                    if isinstance(raw, dict)
                    else raw
                )
                models.append(
                    ProviderModel(
                        provider=str(provider),
                        model=_text(value, field="model id", maximum=200),
                    )
                )
        return tuple(models[:200])

    def managed_status(self) -> ManagedRuntimeStatus:
        return _managed_status(self._request("GET", "/api/managed-runtime"))

    def quota(self, provider: ModelProvider, *, refresh: bool = False) -> ManagedQuota:
        path = f"/api/quota/{provider.value}" + ("?refresh=true" if refresh else "")
        return _managed_quota(self._request("GET", path), provider)

    def start_codex_login(self) -> LoginSession:
        payload = self._request("POST", "/api/providers/oauth/openai-codex/start")
        session_id = validate_session_id(
            _text(payload.get("session_id"), field="session id", maximum=80)
        )
        expires = payload.get("expires_in")
        interval = payload.get("poll_interval")
        if type(expires) is not int or not 1 <= expires <= 1800:
            raise HermesControlError("Hermes login expiry is invalid")
        if type(interval) is not int or not 1 <= interval <= 30:
            raise HermesControlError("Hermes poll interval is invalid")
        return LoginSession(
            session_id=session_id,
            status="pending",
            user_code=_text(payload.get("user_code"), field="user code", maximum=64),
            verification_url=_verification_url(payload.get("verification_url")),
            expires_in=expires,
            poll_interval=interval,
        )

    def poll_codex_login(self, session_id: str) -> LoginSession:
        resolved = validate_session_id(session_id)
        payload = self._request(
            "GET", f"/api/providers/oauth/openai-codex/poll/{resolved}"
        )
        status = payload.get("status")
        if status not in {"pending", "approved", "denied", "error", "expired"}:
            raise HermesControlError("Hermes login status is invalid")
        return LoginSession(
            session_id=resolved,
            status=str(status),
            user_code=None,
            verification_url=None,
            expires_in=None,
            poll_interval=5,
        )

    def cancel_codex_login(self, session_id: str) -> Cancellation:
        resolved = validate_session_id(session_id)
        payload = self._request("DELETE", f"/api/providers/oauth/sessions/{resolved}")
        cancelled = payload.get("ok")
        if not isinstance(cancelled, bool):
            raise HermesControlError("Hermes cancellation response is invalid")
        return Cancellation(cancelled=cancelled, session_id=resolved)


class HermesRuntimeClient:
    """Read fixed API-server diagnostics with a companion-only bearer credential."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = _origin(base_url)
        self._token = _text(token, field="API token", maximum=4096)
        self.opener = urllib.request.build_opener(_NoRedirect())

    def _get(
        self,
        path: Literal[
            "/health/detailed",
            "/v1/capabilities",
            "/v1/review-agent/status",
            "/v1/review-agent/quota/openai-codex",
            "/v1/review-agent/quota/anthropic",
        ],
        *,
        refresh: bool = False,
    ) -> dict[str, object]:
        request = urllib.request.Request(
            self.base_url + path + ("?refresh=true" if refresh else ""),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise HermesControlError(
                "Hermes runtime diagnostics are unavailable"
            ) from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise HermesControlError("Hermes runtime response is too large")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HermesControlError("Hermes runtime response is invalid") from exc
        if not isinstance(payload, dict):
            raise HermesControlError("Hermes runtime response is invalid")
        return cast(dict[str, object], payload)

    def managed_status(self) -> ManagedRuntimeStatus:
        return _managed_status(self._get("/v1/review-agent/status"))

    def quota(self, provider: ModelProvider, *, refresh: bool = False) -> ManagedQuota:
        path = (
            "/v1/review-agent/quota/openai-codex"
            if provider is ModelProvider.CODEX
            else "/v1/review-agent/quota/anthropic"
        )
        return _managed_quota(self._get(path, refresh=refresh), provider)

    def status(self) -> HermesRuntimeStatus:
        health = self._get("/health/detailed")
        capabilities = self._get("/v1/capabilities")
        readiness = health.get("readiness")
        features = capabilities.get("features")
        if not isinstance(readiness, dict) or not isinstance(features, dict):
            raise HermesControlError("Hermes runtime diagnostics are invalid")
        raw_checks = cast(dict[str, object], readiness).get("checks")
        if not isinstance(raw_checks, dict):
            raise HermesControlError("Hermes readiness checks are invalid")
        checks = cast(dict[str, object], raw_checks)
        rows: list[dict[str, object]] = []
        for name in _CHECKS:
            check = checks.get(name)
            if not isinstance(check, dict):
                raise HermesControlError("Hermes readiness checks are incomplete")
            rows.append(
                {"name": name, "status": cast(dict[str, object], check).get("status")}
            )
        return _runtime_status(
            {
                "status": health.get("status"),
                "version": health.get("version"),
                "model": capabilities.get("model"),
                "active_agents": health.get("active_agents"),
                "busy": health.get("gateway_busy"),
                "drainable": health.get("gateway_drainable"),
                "chat_available": cast(dict[str, object], features).get(
                    "chat_completions"
                ),
                "checks": rows,
            }
        )

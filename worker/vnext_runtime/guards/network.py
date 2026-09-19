"""The mature network safety policy behind a run-local guard capability.

The guard is deliberately a runtime-only object.  It has no Protocol, Git,
report, recovery, or executor dependency.  A caller may use one instance for
preflight and then register a distinct instance with the exact RunScope that
owns the runtime watchdog.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """A bounded, non-secret result from a runtime guard."""

    allowed: bool
    reason: str
    country: str | None = None


class RunGuard(Protocol):
    """Capability contract for a run-local safety guard."""

    guard_id: str

    def preflight(self, context: object | None = None) -> GuardVerdict:
        """Check safety before a run is claimed."""

    def check(self, context: object | None = None) -> GuardVerdict:
        """Check safety for the run currently owning this guard."""

    def dispose(self) -> None:
        """Stop this guard from affecting any later run."""


class NetworkGuardClosedError(RuntimeError):
    """Raised only when an operation requires a live guard instance."""


ProbeOpener = Callable[..., Any]
ProbeCheck = Callable[[], GuardVerdict]
EventLogger = Callable[[str, dict[str, object]], None]


def network_guard_settings(config: Mapping[str, object]) -> Mapping[str, object] | None:
    raw = config.get("network_guard")
    if not isinstance(raw, Mapping) or not bool(raw.get("enabled", False)):
        return None
    return raw


def _fail_closed_result(settings: Mapping[str, object], reason: str) -> GuardVerdict:
    if bool(settings.get("fail_closed", True)):
        return GuardVerdict(False, reason)
    return GuardVerdict(True, f"{reason}; fail-open policy")


def parse_probe_country(body: str) -> str | None:
    """Parse a country code from Cloudflare trace or a simple JSON probe."""

    for raw_line in body.splitlines():
        key, separator, value = raw_line.partition("=")
        if separator and key.strip().lower() in {"loc", "country", "country_code"}:
            candidate = value.strip().upper()
            if len(candidate) == 2 and candidate.isalpha():
                return candidate
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict):
        for key in ("country", "country_code", "loc"):
            candidate = str(data.get(key, "")).strip().upper()
            if len(candidate) == 2 and candidate.isalpha():
                return candidate
    return None


class NetworkGuard:
    """Fail-closed network policy with a disposable run-local callback.

    ``probe_check`` exists only for the legacy compatibility seam and tests;
    normal production instances execute the policy in this module directly.
    After disposal, the watchdog callback becomes a no-op-safe callback.  It
    cannot report a late unsafe result that could be applied to another run.
    """

    guard_id = "network"

    def __init__(
        self,
        config: Mapping[str, object],
        *,
        opener: ProbeOpener = urlopen,
        probe_check: ProbeCheck | None = None,
        event_logger: EventLogger | None = None,
    ) -> None:
        if not isinstance(config, Mapping):
            raise TypeError("network guard config must be a mapping")
        self._config = dict(config)
        self._opener = opener
        self._probe_check = probe_check
        self._event_logger = event_logger
        self._disposed = False

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def enabled(self) -> bool:
        return network_guard_settings(self._config) is not None

    @property
    def watchdog_interval_seconds(self) -> float:
        settings = network_guard_settings(self._config)
        if settings is None:
            return 10.0
        try:
            return max(1.0, float(settings.get("watchdog_interval_seconds", 10)))
        except (TypeError, ValueError):
            return 10.0

    def preflight(self, context: object | None = None) -> GuardVerdict:
        return self._evaluate("preflight")

    def check(self, context: object | None = None) -> GuardVerdict:
        return self._evaluate("check")

    def watchdog_callback(self) -> Callable[[], tuple[bool, str]]:
        """Return a callback safe to hand to the existing lifecycle seam."""

        def callback() -> tuple[bool, str]:
            if self._disposed:
                return True, "guard disposed"
            verdict = self.check()
            return verdict.allowed, verdict.reason

        return callback

    def dispose(self) -> None:
        self._disposed = True
        self._emit("disposed", {"guard_id": self.guard_id})

    close = dispose

    def _evaluate(self, event: str) -> GuardVerdict:
        if self._disposed:
            return GuardVerdict(True, "guard disposed")
        verdict = (
            self._probe_check()
            if self._probe_check is not None
            else self._check_policy()
        )
        if not isinstance(verdict, GuardVerdict):
            raise TypeError("network guard probe must return a GuardVerdict")
        self._emit(
            event,
            {
                "guard_id": self.guard_id,
                "allowed": verdict.allowed,
                "reason": verdict.reason,
                **({"country": verdict.country} if verdict.country else {}),
            },
        )
        return verdict

    def _check_policy(self) -> GuardVerdict:
        settings = network_guard_settings(self._config)
        if settings is None:
            return GuardVerdict(True, "disabled")

        probe_url = str(settings.get("probe_url", "")).strip()
        if not probe_url.lower().startswith("https://"):
            return _fail_closed_result(settings, "probe URL is not HTTPS")
        try:
            timeout = float(settings.get("timeout_seconds", 5))
            if timeout <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return _fail_closed_result(settings, "probe timeout is invalid")

        request = Request(
            probe_url,
            headers={"User-Agent": "AI-Agent-Bridge-network-guard/1"},
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                status_value = getattr(response, "status", None)
                status = int(
                    status_value if status_value is not None else response.getcode()
                )
                body = response.read(16384).decode("utf-8", errors="replace")
        except HTTPError:
            return _fail_closed_result(settings, "probe returned HTTP error")
        except (URLError, TimeoutError, OSError, ValueError):
            return _fail_closed_result(settings, "probe failed or timed out")

        if status < 200 or status >= 300:
            return _fail_closed_result(settings, f"probe returned HTTP {status}")

        country = parse_probe_country(body)
        if country is None:
            return _fail_closed_result(settings, "probe country is unknown or malformed")

        blocked = {
            str(code).strip().upper()
            for code in settings.get("blocked_country_codes", ["CN"])
            if str(code).strip()
        }
        allowed_codes = {
            str(code).strip().upper()
            for code in settings.get("allowed_country_codes", [])
            if str(code).strip()
        }
        if country in blocked:
            return GuardVerdict(False, f"country blocked: {country}", country)
        if allowed_codes and country not in allowed_codes:
            return GuardVerdict(False, f"country not allowed: {country}", country)
        return GuardVerdict(True, f"country allowed: {country}", country)

    def _emit(self, event: str, fields: dict[str, object]) -> None:
        if self._event_logger is None:
            return
        safe_fields = {
            key: value
            for key, value in fields.items()
            if key in {"guard_id", "allowed", "reason", "country"}
        }
        if "reason" in safe_fields:
            safe_fields["reason"] = _safe_diagnostic_reason(safe_fields["reason"])
        try:
            self._event_logger(f"network_guard_{event}", safe_fields)
        except Exception:
            # Diagnostics are ancillary and must never change guard policy.
            return


def _safe_diagnostic_reason(value: object) -> str:
    """Keep guard event diagnostics bounded without copying probe/config data."""

    reason = value if isinstance(value, str) else ""
    if reason in {
        "disabled",
        "probe URL is not HTTPS",
        "probe timeout is invalid",
        "probe returned HTTP error",
        "probe failed or timed out",
        "probe country is unknown or malformed",
        "guard disposed",
    }:
        return reason
    if re.fullmatch(r"probe returned HTTP [0-9]{3}", reason):
        return reason
    if re.fullmatch(r"country (?:blocked|not allowed): [A-Z]{2}", reason):
        return reason
    if re.fullmatch(r"country allowed: [A-Z]{2}", reason):
        return reason
    if reason.endswith("; fail-open policy"):
        base = reason[: -len("; fail-open policy")]
        return _safe_diagnostic_reason(base) + "; fail-open policy"
    return "network guard verdict unavailable"

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from src.errors import ConfigurationError

logger = logging.getLogger(__name__)

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


class StartupCheckMode(StrEnum):
    OFF = "off"
    WARN = "warn"
    STRICT = "strict"


class StartupCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectionCheck:
    name: str
    status: str
    detail: str


def describe_failure(exc: Exception) -> str:
    if isinstance(exc, ConfigurationError):
        return str(exc)
    # Exception messages from HTTP clients embed the request URL, which carries bot tokens and webhook secrets.
    status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(
        getattr(exc, "resp", None), "status", None
    )
    return f"{type(exc).__name__} (HTTP {status})" if status else type(exc).__name__


def _run_one(name: str, probe: Callable[[], str] | None) -> ConnectionCheck:
    if probe is None:
        return ConnectionCheck(name, SKIPPED, "not configured")
    try:
        return ConnectionCheck(name, OK, probe())
    except Exception as exc:  # noqa: BLE001 - probes span several SDKs; report any failure, never crash startup
        return ConnectionCheck(name, FAILED, describe_failure(exc))


def _log(check: ConnectionCheck) -> None:
    level = {OK: logging.INFO, SKIPPED: logging.WARNING, FAILED: logging.ERROR}[check.status]
    logger.log(level, "Connection check [%s] %s: %s", check.name, check.status.upper(), check.detail)


_STATUS_EMOJI = {OK: "✅", FAILED: "❌", SKIPPED: "⏭️"}


def format_status_report(checks: list[ConnectionCheck]) -> str:
    lines = [f"{_STATUS_EMOJI[check.status]} {check.name}: {check.detail}" for check in checks]
    return "\n".join(["👋 Bonjour, je suis ton assistant Gmail. Je viens de démarrer.", "", *lines])


def run_startup_checks(
    probes: Mapping[str, Callable[[], str] | None], mode: StartupCheckMode
) -> list[ConnectionCheck]:
    """A probe of None means the service is not configured; probes must be read-only."""
    if mode is StartupCheckMode.OFF:
        return []

    results = [_run_one(name, probe) for name, probe in probes.items()]
    for check in results:
        _log(check)

    failed = [check.name for check in results if check.status == FAILED]
    if failed and mode is StartupCheckMode.STRICT:
        raise StartupCheckError(f"Startup connection checks failed: {', '.join(failed)}")
    return results

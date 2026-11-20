"""Чтение и переключение override клиентского балансера через Xray API."""

import json
from dataclasses import dataclass
from subprocess import TimeoutExpired

from xray_vps_manager.core import process
from xray_vps_manager.core.errors import ExternalCommandError
from xray_vps_manager.core.paths import paths
from xray_vps_manager.xray.client_routes import XRAY_API_SERVER


_SWITCH_FAILED = "Runtime route switch failed. Run xray-client sync-routes first and check Xray RoutingService."


@dataclass(frozen=True)
class BalancerState:
    override: str | None
    candidates: tuple[str, ...]


def read_balancer(balancer: str) -> BalancerState:
    command = [str(paths().xray_bin), "api", "bi", f"--server={XRAY_API_SERVER}", "-json", balancer]
    try:
        result = process.run(command, timeout=8, check=True)
        # Xray v26.3.27 сериализует пустой target как объект override без поля target.
        state = json.loads(result.stdout)["balancer"]
        override = state["override"]
        target = override.get("target", "") if isinstance(override, dict) else None
        if not isinstance(target, str):
            raise ValueError("Invalid override target in Xray API response")
        candidates = state.get("principleTarget", {}).get("tag", [])
        if not isinstance(candidates, list) or not all(isinstance(tag, str) for tag in candidates):
            raise ValueError("Invalid balancer candidates in Xray API response")
    except (ExternalCommandError, OSError, TimeoutExpired, ValueError, KeyError, TypeError, AttributeError) as error:
        raise ExternalCommandError(_SWITCH_FAILED, detail=getattr(error, "detail", "") or str(error)) from error
    return BalancerState(target or None, tuple(sorted(candidates)))


def set_override(balancer: str, target: str | None) -> None:
    base = [str(paths().xray_bin), "api", "bo"]
    args = ["-b", balancer]
    if target is None:
        args.append("-r")
    else:
        if target.startswith("-"):
            args.append("--")
        args.append(target)
    command = [*base, f"--server={XRAY_API_SERVER}", *args]
    try:
        result = process.run(command, timeout=8)
        detail = result.stderr or result.stdout or f"exit {result.returncode}"
        # Запасной синтаксис безопасен только при отказе разбора до отправки RPC.
        if result.returncode and "flag provided but not defined: -server" in detail:
            result = process.run([*base, "-s", XRAY_API_SERVER, *args], timeout=8)
            detail = result.stderr or result.stdout or f"exit {result.returncode}"
    except (OSError, TimeoutExpired) as error:
        raise ExternalCommandError(_SWITCH_FAILED, detail=str(error)) from error
    if result.returncode:
        raise ExternalCommandError(_SWITCH_FAILED, detail=detail.strip())

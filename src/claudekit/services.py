"""Control the host's own systemd units from the UI.

Only units the host declared in `KitConfig.services` can be touched, and only with the actions
that spec allows. The client sends a logical key ("backend"), never a unit name — so no request
can reach a unit the host didn't opt in to.

Privileges: the app runs unprivileged, so `systemctl restart` needs an explicit grant. Use
`sudoers_snippet()` to generate a minimal, per-unit NOPASSWD rule.
"""

from __future__ import annotations

import shutil
import subprocess

from .config import KitConfig, ServiceSpec

READ_ACTIONS = ("status", "is-active", "is-enabled")
WRITE_ACTIONS = ("start", "stop", "restart", "reload")
ALL_ACTIONS = READ_ACTIONS + WRITE_ACTIONS


def _systemctl() -> str | None:
    return shutil.which("systemctl")


def _argv(action: str, unit: str, use_sudo: bool) -> list[str]:
    sc = _systemctl() or "systemctl"
    argv = [sc, action, unit]
    if use_sudo and action in WRITE_ACTIONS:
        argv = ["sudo", "-n", *argv]
    return argv


def describe(cfg: KitConfig) -> list[dict]:
    """Current state of every declared service (never raises — a broken unit shows as unknown)."""
    out = []
    for key, spec in cfg.services.items():
        out.append({
            "key": key,
            "label": spec.label,
            "unit": spec.unit,
            "actions": list(spec.actions),
            "active": _read(spec.unit, "is-active"),
            "enabled": _read(spec.unit, "is-enabled"),
        })
    return out


def _read(unit: str, action: str) -> str:
    if not _systemctl():
        return "unknown"
    try:
        p = subprocess.run([_systemctl(), action, unit],
                           capture_output=True, text=True, timeout=10)
        return (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr) else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def act(cfg: KitConfig, key: str, action: str, use_sudo: bool = True) -> dict:
    """Run `action` on the service registered under `key`."""
    spec: ServiceSpec | None = cfg.services.get(key)
    if spec is None:
        raise KeyError(f"unknown service: {key}")
    if action not in ALL_ACTIONS:
        raise ValueError(f"unsupported action: {action}")
    if action not in spec.actions:
        raise PermissionError(f"action '{action}' not allowed for service '{key}'")
    if not _systemctl():
        raise RuntimeError("systemctl not available on this host")

    argv = _argv(action, spec.unit, use_sudo)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "key": key, "action": action, "error": "timed out"}

    out = (p.stdout or "") + (p.stderr or "")
    ok = p.returncode == 0
    result = {"ok": ok, "key": key, "action": action, "unit": spec.unit,
              "returncode": p.returncode, "output": out.strip()[:4000]}
    if not ok and ("password is required" in out or "not permitted" in out.lower()):
        # The single most likely failure — say exactly how to fix it rather than surfacing
        # a bare "exit 1" the user has to decode.
        result["hint"] = ("the app may not run systemctl unattended; install the rule from "
                          "`claudekit.services.sudoers_snippet(cfg)`")
    return result


def restart_self_safe(cfg: KitConfig, key: str) -> dict:
    """Restart a unit that may be the caller's own.

    `systemctl restart` on your own unit kills the process mid-request, so the HTTP response never
    arrives. We ask systemd to do it detached, letting the response flush first.
    """
    spec = cfg.services.get(key)
    if spec is None:
        raise KeyError(f"unknown service: {key}")
    if "restart" not in spec.actions:
        raise PermissionError(f"restart not allowed for '{key}'")
    sc = _systemctl()
    if not sc:
        raise RuntimeError("systemctl not available on this host")
    argv = ["sudo", "-n", sc, "--no-block", "restart", spec.unit]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return {"ok": p.returncode == 0, "key": key, "unit": spec.unit,
                "output": ((p.stdout or "") + (p.stderr or "")).strip()[:2000]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "key": key, "error": "timed out"}


def sudoers_snippet(cfg: KitConfig, user: str) -> str:
    """A minimal sudoers rule granting only the declared write actions on the declared units."""
    sc = _systemctl() or "/usr/bin/systemctl"
    lines = [
        f"# claudeKit — allow {user} to manage only these units, no password.",
        f"# Install as root:  visudo -f /etc/sudoers.d/claudekit-{cfg.app_name}",
        "",
    ]
    for spec in cfg.services.values():
        for action in spec.actions:
            if action in WRITE_ACTIONS:
                lines.append(f"{user} ALL=(root) NOPASSWD: {sc} {action} {spec.unit}")
                lines.append(f"{user} ALL=(root) NOPASSWD: {sc} --no-block {action} {spec.unit}")
    return "\n".join(lines) + "\n"

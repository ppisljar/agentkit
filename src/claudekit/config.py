"""Host-app configuration. A host wires claudeKit up by building one `KitConfig` and passing it
to the store, the agent runner, the scheduler and the HTTP router.

Nothing in the kit reads module-level globals or guesses paths — everything a component needs
arrives through this object. That is the difference between this and the HomeFlix original, whose
modules imported `common.ROOT` directly and rewrote hardcoded dev-machine paths at runtime.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AgentSpec:
    """A Claude Code agent the host exposes.

    `schedule` is descriptive for the UI ("daily", "on demand", "on approve"); actual scheduling
    is driven by the scheduler's `schedule_source` rows, so a host can change it without code.
    `script` is an optional host script run by the scheduler instead of a direct prompt.
    """

    name: str
    label: str
    description: str
    system: str
    prompt: str
    schedule: str = "on demand"
    model: str = "sonnet"
    script: str | None = None
    # Extra directories the agent may read (beyond the app root).
    add_dirs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceSpec:
    """A systemd unit the UI may control.

    `actions` is an explicit allowlist — a host that only wants restarts should not implicitly
    grant stop. The kit never accepts a unit name from the client; callers reference the logical
    key, which is resolved here.
    """

    key: str
    unit: str
    label: str
    actions: tuple[str, ...] = ("restart", "status")


@dataclass(frozen=True)
class ConfigFileSpec:
    """A file the UI may read and (optionally) write.

    Paths are resolved against the app root and re-checked on every access, so a host cannot be
    tricked into exposing something outside it.
    """

    key: str
    path: str
    label: str
    editable: bool = True
    # 'env' renders as key/value pairs; 'text' as a plain editor; 'json'/'yaml' get validated.
    format: str = "text"
    # Keys whose values are masked in API responses (secrets stay server-side).
    secret_keys: tuple[str, ...] = ()


@dataclass
class KitConfig:
    root: Path
    data_dir: Path
    app_name: str = "app"
    python_bin: str = field(default_factory=lambda: sys.executable)
    claude_bin: str = "claude"
    db_path: Path | None = None
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    services: dict[str, ServiceSpec] = field(default_factory=dict)
    config_files: dict[str, ConfigFileSpec] = field(default_factory=dict)
    # Scrapers/crawlers the scheduler can run: key -> argv template (relative to root).
    adapters: dict[str, list[str]] = field(default_factory=dict)
    # Tools denied to every agent. Agents run unattended, so this is deliberately restrictive.
    deny_tools: tuple[str, ...] = (
        "Bash(rm:*)", "Bash(git push:*)", "Bash(kill:*)", "Bash(pkill:*)",
        "Bash(shutdown:*)", "Bash(reboot:*)", "Bash(systemctl:*)", "Bash(sudo:*)",
    )
    agent_timeout_sec: int = 2700
    keep_transcripts: int = 60

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.data_dir = Path(self.data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.db_path is None:
            self.db_path = self.data_dir / "claudekit.db"
        self.db_path = Path(self.db_path)

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "agent_history"

    def resolve_in_root(self, relpath: str) -> Path:
        """Resolve `relpath` under the app root, refusing anything that escapes it."""
        p = (self.root / relpath).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"path escapes app root: {relpath}")
        return p

    def claude_available(self) -> bool:
        return shutil.which(self.claude_bin) is not None

    def env(self) -> dict:
        e = dict(os.environ)
        e.setdefault("CLAUDEKIT_APP", self.app_name)
        return e

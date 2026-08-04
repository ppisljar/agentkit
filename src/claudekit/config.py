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
    # A host script (argv, relative to root) run INSTEAD of calling the agent directly. Use this
    # when the run needs domain work around the model call — building prompt context from live
    # data beforehand, or deriving a factual report from the database afterwards. The script owns
    # the whole run: it typically calls claudekit.agent_run.run() itself and writes its own report.
    script: list[str] | str | None = None
    # Extra directories the agent may read (beyond the app root).
    add_dirs: tuple[str, ...] = ()
    # Skip the shared KitConfig.system_hints for this agent (e.g. a conversational agent that a
    # human really does reply to, for which a "you get one turn" hint would be wrong).
    no_hints: bool = False
    # Task to run when items THIS agent raised are approved/answered, instead of handing them to
    # the generic apply agent. An agent whose findings are domain-specific (a download pipeline, a
    # tracker) knows how to act on them; a general apply agent would have to rediscover all of it.
    # The named task is expected to be a maintenance-only run — carry out the decisions, nothing
    # else — so it is usually a second AgentSpec wrapping the same script with a flag.
    decisions_task: str | None = None


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
    # The second line is about TURN STRUCTURE rather than destruction: an agent run is a single
    # `claude -p` turn with nobody to notify it, so an agent that schedules a wakeup or waits on a
    # monitor simply ends its turn and dies with the work unfinished. Denying them turns a silent
    # stall into an immediate tool error the agent can recover from.
    deny_tools: tuple[str, ...] = (
        "Bash(rm:*)", "Bash(git push:*)", "Bash(kill:*)", "Bash(pkill:*)",
        "Bash(shutdown:*)", "Bash(reboot:*)", "Bash(systemctl:*)", "Bash(sudo:*)",
        "ScheduleWakeup", "Monitor", "CronCreate",
    )
    # Text appended to EVERY agent's system prompt (unless AgentSpec.no_hints). Appended at resolve
    # time rather than baked into each spec, so it also reaches prompts the user has customised in
    # the UI — a host-wide rule shouldn't be lost the moment someone edits one agent's prompt.
    system_hints: tuple[str, ...] = ()
    # fn(task) -> Any, called just before a task runs. Whatever it returns is handed back to
    # post_run as info["pre"], so a host can measure the world before and after a run without the
    # kit knowing what is being measured (row counts, file sizes, a timestamp to diff against…).
    pre_run: object | None = None
    # Called after a scheduler task finishes: fn(task, status, info). `info` carries result, error,
    # duration_sec, new_count and pre. Lets a host trigger follow-up work — e.g. rebuild a static
    # site, but only when the run actually produced something.
    # Exceptions are logged and swallowed; a failing hook must not fail the task.
    post_run: object | None = None
    # Sugar over pre_run/post_run for the common case: fn(task) -> int | None, the app's item count
    # for `task` right now. Called around adapter runs; the delta is stored as last_new_count (the
    # "+N new" the UI shows) and passed to post_run. Anything more involved than a count should use
    # pre_run/post_run directly — this exists because the count needs a home in the schema.
    count_items: object | None = None
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

    # Where `claude` commonly installs. Searched only when it isn't on PATH: a scheduler daemon
    # usually runs under systemd with an explicitly pinned PATH that omits ~/.local/bin, so an
    # agent would report "CLI not found" while the CLI was installed and logged in.
    _CLAUDE_DIRS = ("~/.local/bin", "~/.claude/local", "/usr/local/bin", "/opt/homebrew/bin")

    def resolve_claude_bin(self) -> str | None:
        """Absolute path to the Claude Code CLI, or None if it genuinely isn't installed."""
        if os.path.isabs(self.claude_bin):
            return self.claude_bin if os.access(self.claude_bin, os.X_OK) else None
        found = shutil.which(self.claude_bin)
        if found:
            return found
        for d in self._CLAUDE_DIRS:
            p = os.path.join(os.path.expanduser(d), self.claude_bin)
            if os.access(p, os.X_OK):
                return p
        return None

    def claude_available(self) -> bool:
        return self.resolve_claude_bin() is not None

    def env(self) -> dict:
        e = dict(os.environ)
        e.setdefault("CLAUDEKIT_APP", self.app_name)
        return e

"""claudeKit — a reusable platform layer for self-hosted apps.

Gives a host application, in one wiring step:

* Claude Code **agents** with editable prompts (defaults in code, overrides in the database)
* a **report / approval loop** — agents investigate read-only and raise questions and proposals;
  a second agent carries out only what the human approved
* a **scheduler** for scrapers ("adapters") and daily agents, with on-demand runs and live job logs
* **service control** (restart your own systemd units) and **config-file editing**, both restricted
  to what the host explicitly declared

Typical wiring:

    from claudekit import KitConfig, AgentSpec, ServiceSpec, ConfigFileSpec, Kit

    kit = Kit(KitConfig(
        root=REPO_ROOT, data_dir=REPO_ROOT / "data", app_name="avtonet",
        agents={...}, services={...}, config_files={...}, adapters={...},
    ))
    kit.start()
    app.include_router(kit.fastapi_router())
"""

from __future__ import annotations

from .config import AgentSpec, ConfigFileSpec, KitConfig, ServiceSpec
from .scheduler import Scheduler
from .store import Store

__all__ = [
    "AgentSpec", "ConfigFileSpec", "KitConfig", "ServiceSpec",
    "Kit", "Scheduler", "Store", "__version__",
]

__version__ = "0.1.0"


class Kit:
    """Everything wired together — the object a host app holds onto."""

    def __init__(self, cfg: KitConfig) -> None:
        self.cfg = cfg
        self.store = Store(cfg)
        self.scheduler = Scheduler(cfg, self.store)

    def start(self, run_scheduler: bool = True) -> None:
        """Create tables, register declared tasks and optionally start the scheduler loop.

        Pass `run_scheduler=False` in the web process when the scheduler runs as a separate
        daemon (see `claudekit.daemon`) — two live loops would race to claim the same task.
        """
        self.store.connect()
        self.scheduler.sync_tasks()
        if run_scheduler:
            self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()

    def fastapi_router(self, prefix: str = "/api/kit"):
        from .http.fastapi_router import build_router  # noqa: PLC0415 — optional dependency
        return build_router(self, prefix=prefix)

    def flask_blueprint(self, prefix: str = "/api/kit"):
        from .http.flask_bp import build_blueprint  # noqa: PLC0415 — optional dependency
        return build_blueprint(self, prefix=prefix)

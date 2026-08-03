"""Standalone scheduler daemon.

Runs the scheduler loop in its own process so a web-server restart (a deploy, a crash, an
`Edit`-then-reload) does not interrupt a running crawl or agent. The web process and the daemon
share nothing but the SQLite database:

* the daemon owns the loop, runs the work and writes results
* the web process reads state, and asks for an immediate run by setting `run_requested`

Host entry point:

    from kit_config import KIT
    from claudekit import daemon
    daemon.run(KIT)

The web process should construct its Kit with `Kit.start(run_scheduler=False)` so only one loop
is live; two would race to claim the same task.
"""

from __future__ import annotations

import signal
import sys

from .scheduler import HEARTBEAT_KEY


def run(kit, *, quiet: bool = False) -> int:
    """Run the scheduler loop until SIGINT/SIGTERM. Returns a process exit code."""
    store, sched = kit.store, kit.scheduler
    store.connect()

    if not quiet:
        n_ad, n_ag = len(kit.cfg.adapters), len(kit.cfg.agents)
        print(f"claudekit scheduler for {kit.cfg.app_name}: "
              f"{n_ad} adapters, {n_ag} agents, db={kit.cfg.db_path}", flush=True)

    def _shutdown(signum, _frame):
        if not quiet:
            print(f"received signal {signum}, stopping", flush=True)
        sched.stop()
        # Clear the heartbeat immediately so the UI reports "down" rather than waiting for it
        # to go stale.
        try:
            store.set_meta(HEARTBEAT_KEY, "0")
        except Exception:  # noqa: BLE001 — shutting down anyway
            pass

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        sched.run_forever()
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)
    return 0


def main() -> int:
    """`python -m claudekit.daemon <module>` — import a host module exposing `KIT` and run it."""
    if len(sys.argv) < 2:
        print("usage: python -m claudekit.daemon <host_module_exposing_KIT>", file=sys.stderr)
        return 2
    import importlib

    mod = importlib.import_module(sys.argv[1])
    kit = getattr(mod, "KIT", None)
    if kit is None:
        print(f"{sys.argv[1]} does not expose a KIT object", file=sys.stderr)
        return 2
    return run(kit)


if __name__ == "__main__":
    raise SystemExit(main())

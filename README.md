# claudeKit

A reusable platform layer for self-hosted apps. Wire it into a host application and you get:

- **Claude Code agents** with prompts editable from the UI (defaults in code, overrides in the database)
- **A report / approval loop** — agents investigate read-only and raise *questions* and *proposals*; a second agent carries out only what you approved
- **A scheduler** for scrapers ("adapters") and daily agents, with on-demand runs and live job logs
- **Service control** — restart your own systemd units from the UI
- **Config-file editing** — with secret masking and atomic writes

Extracted from the HomeFlix media server, where this machinery grew up, and generalised so a second
app doesn't have to rebuild it.

## Design

The core is **framework-free**. Nothing under `src/claudekit/` imports Flask or FastAPI; HTTP is a
thin adapter in `http/`. Everything a component needs arrives through one `KitConfig` object rather
than module-level globals — that is the main departure from the original, whose modules imported
paths directly and rewrote hardcoded dev-machine paths at runtime.

```
src/claudekit/
├── config.py       KitConfig, AgentSpec, ServiceSpec, ConfigFileSpec
├── store.py        SQLite schema (ck_* tables) + thread-local connections
├── agent_run.py    headless `claude -p` runner, transcript capture + parsing
├── registry.py     agent defaults + database overrides
├── jobs.py         background jobs with streamed logs
├── reports.py      the report / question / proposal / approval loop
├── scheduler.py    interval adapters + daily agents
├── services.py     systemd control (allowlisted) + sudoers generator
├── configfiles.py  read/write declared config files, secrets masked
└── http/
    ├── fastapi_router.py
    └── flask_bp.py
ui/src/             React + Tailwind components (KitSettings, KitReports)
```

## Wiring a host app

```python
from claudekit import AgentSpec, ConfigFileSpec, Kit, KitConfig, ServiceSpec

KIT = Kit(KitConfig(
    root=REPO_ROOT,
    data_dir=REPO_ROOT / ".kit",
    app_name="myapp",
    agents={"maintenance": AgentSpec(...), "applydecisions": AgentSpec(...)},
    adapters={"scraper": [PY, "run_scraper.py", "--site", "example.com"]},
    services={"backend": ServiceSpec(key="backend", unit="myapp.service",
                                     label="Backend", actions=("restart", "status"))},
    config_files={"env": ConfigFileSpec(key="env", path=".env", label="Config",
                                        format="env", secret_keys=("API_KEY",))},
))

KIT.start()
app.include_router(KIT.fastapi_router())      # or KIT.flask_blueprint()
```

Frontend:

```tsx
import { KitSettings } from '@claudekit/KitSettings'
import { KitReports } from '@claudekit/KitReports'
```

Alias `@claudekit` to `claudeKit/ui/src` in your bundler, and add that path to your Tailwind
`content` globs or the classes get purged.

## The safety model

This is the part worth understanding before pointing agents at a machine you care about.

1. The **investigating** agent (health check, improvements) is instructed to be read-only. It may
   perform only explicitly-listed safe actions. Anything risky it wants becomes a *proposal*.
2. You **approve or reject** each proposal, and answer each question, in the Reports UI.
3. The **apply** agent receives only the approved/answered items and is told to do nothing else.

So an agent never acts on its own conclusions. `KitConfig.deny_tools` additionally denies
destructive tools at the CLI level (`rm`, `git push`, `kill`, `pkill`, `systemctl`, `sudo`,
`shutdown`, `reboot`).

Agents still run with `--dangerously-skip-permissions`, because they run unattended. Treat the
denylist as a backstop, not a sandbox: an agent can still write files inside the app root. Point
this at projects you'd let a capable junior engineer edit unsupervised, not at anything holding
credentials you can't rotate.

## Service control privileges

The app runs unprivileged, so `systemctl restart` needs an explicit grant. Generate a minimal rule:

```python
from claudekit import services
print(services.sudoers_snippet(KIT.cfg, "youruser"))
```

It emits NOPASSWD lines only for the units and actions you declared — a service specced with
`actions=("status",)` gets no sudoers entry at all. Install with
`visudo -f /etc/sudoers.d/claudekit-<app>`.

Restarts use `systemctl --no-block` so a service can restart *itself* — the HTTP response flushes
before the process goes away.

## Storage

All tables are prefixed `ck_` and live in `KitConfig.db_path` (default `<data_dir>/claudekit.db`),
so they can share a host's existing SQLite file without colliding.

| Table | Holds |
|---|---|
| `ck_reports` | one row per agent run |
| `ck_report_items` | questions and proposals, with their status |
| `ck_agent_config` | per-agent prompt/hour overrides |
| `ck_jobs` | background jobs and their logs |
| `ck_schedule_task` | adapters and scheduled agents |

## Known limitations

- **The scheduler runs in-process.** A host restart interrupts an in-flight run; those jobs are
  reported as `interrupted` rather than hanging. HomeFlix used a separate daemon so crawls survived
  a web-server reload — port that if you need it.
- **No auth.** The router assumes the host app protects its own admin routes. Mount it behind
  whatever authentication you already have; it does not add any.
- **`yaml` config validation is optional** — install `pyyaml` or malformed YAML is written as-is.

## Tests

```
pip install -e '.[dev]' && pytest
```

15 tests cover the store, registry overrides, the report/approval loop, job lifecycle including
failures, config masking and path-escape refusal, and scheduler task registration — none require
network, Claude Code or systemd.

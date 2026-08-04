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
ui/src/             React + Tailwind components (KitSettings, KitReports, KitTranscript)
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

<KitReports agentLabels={{ selfcheck: 'Daily health check' }} />
```

`KitSettings` embeds `KitTranscripts` per agent: the captured `.jsonl` for each past run, rendered
as turns with collapsible tool calls and their results. Nothing is fetched until it is opened.

Alias `@claudekit` to `claudeKit/ui/src` in your bundler, and add that path to your Tailwind
`content` globs or the classes get purged.

## Agents that need code around the model call

Some runs can't be "send a prompt, parse the JSON". They need to assemble prompt context from live
data first, or derive a **factual** report from the database afterwards rather than trusting the
model to describe what it did. Give such an agent a `script`:

```python
AgentSpec(name="sldubs", ..., schedule="daily", script=["scripts/sl_dubs_agent.py"])
```

The scheduler then runs `python_bin script...` from the app root and supervises the process (its
output is streamed into the job log); the script calls `claudekit.agent_run.run()` itself and writes
its own report. Without `script`, the agent is called directly and its JSON block becomes the report.

## Hooks around a run: `pre_run` / `post_run`

```python
def after(task, status, info):
    if task == "scraper" and status == "ok":
        rebuild_site()

KitConfig(..., post_run=after)
```

Fires once for every finished task — adapter, agent and agent-script alike. A hook that raises is
logged and swallowed: follow-up work failing must not retroactively mark a scrape that worked as
failed.

`pre_run(task)` runs just before the task; whatever it returns comes back as `info["pre"]`, so a
host can measure the world either side of a run without the kit knowing what is being measured.
Both hooks are advisory — one that raises is logged, and never fails or blocks the run.

`info` carries `result`, `error`, `duration_sec`, `new_count` and `pre`. Counting is common enough
to have a shortcut, because the number needs a home in the schema and the UI:

```python
KitConfig(..., count_items=lambda task: db.count(task))
```

It's called before and after each adapter run; the delta lands in `ck_schedule_task.last_new_count`
(the "+N new" the UI shows) and in `info["new_count"]`, so a hook can skip expensive follow-up work
when a run changed nothing. Counting is advisory — a failing counter never fails the run.

## Host-wide prompt rules: `system_hints`

```python
KitConfig(..., system_hints=(ONESHOT_HINT, DOCS_HINT))
```

Appended to every agent's system prompt at resolve time, so the rule also reaches prompts a user has
edited in the UI — baking hints into each `AgentSpec` means customising one agent silently drops
them. Appending is idempotent, and an agent can opt out with `AgentSpec(no_hints=True)` (a
conversational agent shouldn't be told nobody will ever reply to it).

## Scheduling everything at once

`POST /schedule/run` with no task runs every enabled, unpaused one and reports what it started
versus skipped. `POST /schedule/interval {sec}` sets one crawl interval across all adapters.

That second one is a **write-through**, not a fallback: a task whose `interval_sec` is NULL never
reschedules, so "inherits the global" would be a silent trap where adapters run once and go quiet.
Agents are left alone — they are scheduled by hour.

## The safety model

This is the part worth understanding before pointing agents at a machine you care about.

1. The **investigating** agent (health check, improvements) is instructed to be read-only. It may
   perform only explicitly-listed safe actions. Anything risky it wants becomes a *proposal*.
2. You **approve or reject** each proposal, and answer each question, in the Reports UI.
3. The **apply** agent receives only the approved/answered items and is told to do nothing else.

So an agent never acts on its own conclusions. `KitConfig.deny_tools` additionally denies
destructive tools at the CLI level (`rm`, `git push`, `kill`, `pkill`, `systemctl`, `sudo`,
`shutdown`, `reboot`) plus the *wait-for-a-callback* tools (`ScheduleWakeup`, `Monitor`,
`CronCreate`). The latter aren't destructive — they're structural: a run is a single `claude -p`
turn with nobody to notify it, so an agent that schedules a wakeup ends its turn and dies with the
work unfinished. Denying them turns a silent stall into a recoverable tool error.

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

## Running the scheduler as its own process

By default `Kit.start()` runs the scheduler loop in a background thread inside the host process,
which is fine for development. In production a web-server restart would then kill any in-flight
scrape or agent run — so run the loop separately:

```python
# web process
KIT.start(run_scheduler=False)      # registers tasks, does NOT own the loop
```

```python
# scheduler process:  python -m claudekit.daemon kit_config
from kit_config import KIT
from claudekit import daemon
daemon.run(KIT)
```

The two processes share nothing but the SQLite database. The web process records a request
(`ck_schedule_task.run_requested`); the daemon claims it on its next tick (2s), clears the marker
so it runs exactly once, and does the work. `Scheduler.alive` reads a heartbeat rather than a
thread handle, so the web process can report the daemon's health without sharing memory with it.

Run exactly one loop. Two would race to claim the same task.

## Many projects at once: `claudekit.fleet`

Once a box runs several claudeKit apps, "is anything waiting on me?" means opening each one's
Reports page in turn. `claudekit.fleet` merges them into a single report list, addressed by a
**fleet id** — `"<project key>:<local id>"`, e.g. `homeflix:23`.

```python
from claudekit.fleet import Fleet
from claudekit.http.fleet_router import build_router

fleet = Fleet.from_json("projects.json")
app.include_router(build_router(fleet, prefix="/api/fleet"))
```

```json
[{"key": "homeflix", "label": "HomeFlix",
  "db": "/srv/HomeFlix/data/app.db", "url": "/homeflix/", "color": "#e50914"}]
```

The project list is data rather than a convention because the database path genuinely differs per
host: an app may keep the `ck_` tables in its own database or in a dedicated one.

It reads each project's SQLite file **directly** rather than proxying its HTTP API — a project
need not expose one, and reports are most useful when a project's web app is down. Listing
connections are read-only (`file:…?mode=ro`) and short-lived; a project whose database is missing
becomes a visibly-degraded row instead of an error page. The only writes are the two a project's
own web process already performs: a decision on a `ck_report_items` row, and a `run_requested`
stamp asking that project's scheduler to carry it out.

`http/fleet_router.py` mounts the same routes as the single-app router, so the `KitReports`
component serves a fleet with nothing but a different `base` plus a `projects` prop for the
filter. `POST /reports/apply` is the one difference in kind: a fleet host cannot run another
project's agent, so it queues a run request per project and reports what it queued.

Nothing else in the kit imports this module, and hosts that serve one app are unaffected.

## Known limitations

- **No auth.** The router assumes the host app protects its own admin routes. Mount it behind
  whatever authentication you already have; it does not add any.
- **`http/flask_bp.py` has no real consumer yet.** A test asserts it builds and exposes the
  same routes as the FastAPI adapter, but its behaviour is unexercised until HomeFlix
  migrates onto it.
- **Adapters have no post-run hook**, so a scrape cannot trigger follow-up work (a rebuild, a
  re-index). See `TODO.md`.
- **`yaml` config validation is optional** — install `pyyaml` or malformed YAML is written as-is.
- **Job rows are never pruned**; each keeps up to 64KB of log tail.

## Tests

```
pip install -e '.[dev]' && pytest
```

`tests/test_core.py` covers the store, registry overrides, the report/approval loop, job lifecycle
including failures, config masking and path-escape refusal, scheduler task registration, the
cross-process run-request and heartbeat paths, and route parity between the two HTTP adapters.
`tests/test_fleet.py` covers the multi-project view over several real project databases: id
round-tripping, merge order and limits, degraded projects, and that a decision lands in the right
project and wakes only that project's apply task. None require network, Claude Code or systemd.

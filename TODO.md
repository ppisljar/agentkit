# claudeKit — planned work

## Migrate HomeFlix onto the kit

HomeFlix is where this machinery came from, and it is still running its own copy. Migrating it is
the real test of the abstraction: until a second *existing* app adopts it, the seams are only
validated by one greenfield consumer (avtonet).

**Why it was deferred:** avtonet needed the features first, and HomeFlix works today. Nothing is
broken; this is consolidation. The cost of the split is real though — on 2026-08-04 a `run_now`
bug was fixed in HomeFlix that the kit never had, while HomeFlix gained five things the kit lacked.
Fixes do not flow between them.

### What maps directly

| HomeFlix today | Maps to |
|---|---|
| `scripts/agent_run.py` | `claudekit.agent_run` — near-identical, delete the local copy |
| `scripts/jobs.py` | `claudekit.jobs` — near-identical |
| `scripts/agents.py` `DEFAULTS` | a `dict[str, AgentSpec]` in a `kit_config.py` |
| `agents.py` `DOCS_HINT` / `ONESHOT_HINT` / `COMMIT_HINT` / uncommitted-work hints | `KitConfig.system_hints` |
| `scripts/scheduler.py` + `scheduler_daemon.py` | `claudekit.scheduler` + `claudekit.daemon` |
| `self_investigate.py`, `sl_dubs_agent.py`, `imdb_crawl_agent.py`, `improvements_agent.py` | `AgentSpec.script` |
| `_run_build_site()` rebuild-if-N-new | `KitConfig.post_run` |
| `app.db` `reports`, `report_questions`, `agent_config`, `jobs`, `schedule_source` | the `ck_*` tables |
| `serve.py` `/api/agents*`, `/api/reports*`, `/api/schedule*` | `kit.flask_blueprint()` |
| `web/src/components/Settings/*`, `Reports/*` | `KitSettings`, `KitReports` |

### Closed 2026-08-04 — see "Close the HomeFlix gaps"

- `AgentSpec.script` is wired (it was declared, copied in `registry.resolve`, and read nowhere).
  Unblocks all four HomeFlix daily agents, which do domain work around the model call.
- `KitConfig.post_run`, fired once from `_finish` for every finished task of any kind.
- `KitConfig.system_hints`, appended at resolve time so UI-edited prompts keep host-wide rules.
- `deny_tools` gains `ScheduleWakeup` / `Monitor` / `CronCreate` — the stall that cost HomeFlix
  two empty agent runs.
- `resolve_claude_bin()` falls back to `~/.local/bin` etc. when systemd pins PATH.

### Remaining before HomeFlix can move

1. **Data migration.** Easier than previously noted: `ck_reports` and `ck_report_items` are
   *column-identical* to `reports` and `report_questions` (same names, same `kind` question/proposal
   split) — a plain `INSERT ... SELECT` each. `agent_config` → `ck_agent_config` gains an `hour`
   column that HomeFlix keeps in `schedule_meta`, so that has to be folded in. `schedule_source` →
   `ck_schedule_task` needs a `kind` ('adapter'/'agent') derived from `BUILTIN_TASKS` membership,
   and **drops `last_new_count`** (see 3). Rehearse on a copy of `app.db`.

2. **Route compatibility.** The frontend calls `/api/agents`, `/api/reports`, `/api/schedule`; the
   blueprint serves `/api/kit/*`. Either mount at `/api` or update the frontend. Mounting at `/api`
   risks colliding with HomeFlix's own routes — check `/api/jobs`, `/api/health`, `/api/config`
   before choosing.

3. **Scheduler feature delta.** `ck_schedule_task` has no `last_new_count`, which HomeFlix's Sources
   UI shows ("+N new") and which gates the `build_site` rebuild. `post_run` now provides the
   trigger, but the *"did this run produce anything"* signal still has to come from somewhere —
   probably a convention on adapter exit (stdout marker or exit code), since the kit deliberately
   knows nothing about what an adapter scraped.

4. **`flask_bp.py` is unexercised.** Route parity with the FastAPI adapter is asserted by a test,
   but no consumer has run its *behaviour*. HomeFlix would be the first; expect rough edges in
   request parsing and error shapes.

5. **Reports UI parity.** HomeFlix's `ReportsTab` renders findings with severity, filters by agent,
   and has a transcript viewer (`TranscriptViewer.tsx`, which parses the captured `.jsonl` including
   tool calls and their results). Compare `KitReports` feature-for-feature *before* the swap.

6. **Agent-script reporting shape.** With `AgentSpec.script`, a wrapper writes its own report — but
   the call has to move from HomeFlix's `reports.add_report(status, summary, detail, findings=…,
   questions=…)` to `claudekit.reports.save(store, agent, payload, …)`, which takes a parsed payload
   dict rather than positional fields. Mechanical, but every wrapper touches it.

7. **Prompts carry dev-machine paths.** `agents.py` rewrites `/Users/ppisljar/...` at runtime via
   `_localize()`. On migration, prompts should use kit config values and `_localize()` should go.

8. **Two loops must not run at once.** HomeFlix's `scheduler_daemon.py` and the kit's loop would
   race for the same tasks. The cutover must stop the old daemon in the same step that starts the
   new one — and HomeFlix's daemon is spawned by `serve.py`, so that is a `serve.py` change too.

9. **HomeFlix domain code stays put.** Downloads (registry, failure persistence, per-provider and
   torrent concurrency caps, torrent sourcing, sihq, `require_slo_dub`) is not kit material. Only
   the agent/report/settings/scheduler layer moves.

**Suggested order:** rehearse the data migration on a throwaway copy of `app.db` → swap the backend
modules → swap the routes → swap the UI → delete the originals → drop the old tables after a week.

## Other

- **No auth.** The router assumes the host protects its own admin routes.
- **Job log retention.** `ck_jobs.log` keeps the last 64KB per job and rows are never pruned;
  add a retention sweep.
- **Agent scripts report exit status only.** `_run_agent_script` supervises the process; the script
  writes its own report. If a host wants the kit to parse a script's stdout, that needs a convention
  (a trailing JSON block, as the direct-agent path already uses).

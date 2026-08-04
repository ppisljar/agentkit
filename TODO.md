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
   `ck_schedule_task` needs a `kind` ('adapter'/'agent') derived from `BUILTIN_TASKS` membership;
   `last_new_count` now carries across unchanged (see 3). Rehearse on a copy of `app.db`.

2. **Route paths (minor).** The blueprint serves `/api/kit/*`, which cannot collide with HomeFlix's
   own `/api/*`. And `KitSettings`/`KitReports` already call `/api/kit` via the kit's own client, so
   adopting them settles it. The only leftovers are HomeFlix screens that call the old paths
   directly — `SourceCard` and `SchedulerSection` — which just need repointing. Do NOT remount the
   blueprint at `/api` to avoid that; *that* is what would risk collisions.

3. **~~Scheduler feature delta~~ — done.** `last_new_count` is now a column, fed by
   `KitConfig.count_items` (called around each adapter run) and passed to `post_run` as
   `info["new_count"]`, which is what gates the `build_site` rebuild. `pre_run`/`post_run` cover
   anything more involved.

4. **`flask_bp.py` is unexercised.** Route parity with the FastAPI adapter is asserted by a test,
   but no consumer has run its *behaviour*. HomeFlix would be the first; expect rough edges in
   request parsing and error shapes.

5. **~~Reports UI parity~~ — done, and HomeFlix gains from it.** Compared feature-for-feature:
   `KitReports` was already *ahead* (findings as objects with severity badges and detail vs
   HomeFlix's plain strings; live apply-job polling vs fire-and-forget) and already had the
   "N open" badge. Added `agentLabels` for friendly agent names, and `KitTranscript.tsx` — the one
   real gap, since the backend already captured and parsed transcripts but no UI exposed them.

6. **Agent-script reporting shape (eased).** `reports.save()` now takes explicit
   `status`/`summary`/`detail`/`findings` keywords that override the payload, so a wrapper that
   knows the truth can pass it directly. The rewrite from HomeFlix's
   `add_report(status, summary, detail, …)` is now argument-shuffling, but every wrapper still
   touches it — and `questions=`/`proposals=` still have to move into the payload dict.

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

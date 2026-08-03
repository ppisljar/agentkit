# claudeKit — planned work

## Migrate HomeFlix onto the kit (deferred — noted 2026-08-03)

HomeFlix is where this machinery came from, and it is still running its own copy. Migrating it is
the real test of the abstraction: until a second *existing* app adopts it, the seams are only
validated by one greenfield consumer (avtonet).

**Why it was deferred:** avtonet needed the features first, and HomeFlix works today. Nothing is
broken; this is consolidation, not a fix.

**What the migration involves**

| HomeFlix today | Maps to |
|---|---|
| `scripts/agent_run.py` | `claudekit.agent_run` — near-identical, delete the local copy |
| `scripts/jobs.py` | `claudekit.jobs` — near-identical |
| `scripts/agents.py` `DEFAULTS` | a `dict[str, AgentSpec]` in a `kit_config.py` |
| `scripts/scheduler.py` + `scheduler_daemon.py` | `claudekit.scheduler` + `claudekit.daemon` |
| `app.db` tables `reports`, `report_questions`, `agent_config`, `jobs`, `schedule_*` | the `ck_*` tables |
| `serve.py` routes `/api/agents*`, `/api/reports*`, `/api/schedule*` | `kit.flask_blueprint()` |
| `web/src/components/Settings/*`, `Reports/*` | `KitSettings`, `KitReports` |

**Known obstacles**

1. **Flask, not FastAPI.** `http/flask_bp.py` exists and a test asserts route parity with the
   FastAPI adapter, but no consumer exercises its behaviour yet — expect to find rough edges.
2. **Data migration.** HomeFlix has live history in the old tables. Needs a one-off copy into the
   `ck_*` tables (`reports` → `ck_reports`, `report_questions` → `ck_report_items`, noting the
   column rename and the `kind` split into question/proposal).
3. **Prompts carry dev-machine paths.** `agents.py` rewrites `/Users/ppisljar/...` at runtime via
   `_localize()`. On migration those prompts should be rewritten to use the kit's config values
   instead, and `_localize()` dropped.
4. **HomeFlix's scheduler does more.** It has a `build_site` step and "+N new items" logic that
   decides whether to rebuild. That is domain behaviour — it should become a host-supplied hook
   (a post-run callback on an adapter), which the kit does not have yet.
5. **Route compatibility.** HomeFlix's frontend calls `/api/agents`, not `/api/kit/agents`. Either
   mount the blueprint at `/api` or update the frontend.

**Suggested order:** add the post-run hook → migrate the data in a throwaway copy of `app.db` →
swap the backend modules → swap the routes → swap the UI → delete the originals.

## Other

- **No auth.** The router assumes the host protects its own admin routes.
- **Post-run hooks.** Adapters cannot yet trigger follow-up work (see obstacle 4).
- **Job log retention.** `ck_jobs.log` keeps the last 64KB per job and rows are never pruned;
  add a retention sweep.

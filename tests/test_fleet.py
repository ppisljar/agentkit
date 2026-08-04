"""Fleet tests — the multi-project view over several projects' kit databases.

Every project here is a real `Kit` over its own SQLite file, so the tests exercise the actual
schema and the actual cross-process contract (an UPDATE on ck_report_items, a `run_requested`
stamp on ck_schedule_task) rather than a mock of it.
"""

from __future__ import annotations

import json

import pytest

from claudekit import AgentSpec, Kit, KitConfig, reports
from claudekit.fleet import Fleet, Project, ProjectUnavailable, UnknownProject, make_id, split_id


def _make_project(tmp_path, key: str, *, agents=("selfcheck",)) -> Kit:
    """A standalone project with its own database, wired like a real host."""
    root = tmp_path / key
    (root / "data").mkdir(parents=True)
    cfg = KitConfig(
        root=root, data_dir=root / "data", app_name=key,
        agents={name: AgentSpec(name=name, label=name, description="d",
                                system="sys", prompt="pr", schedule="daily")
                for name in agents},
    )
    kit = Kit(cfg)
    kit.store.connect()
    return kit


def _seed(kit: Kit, agent: str, *, created: float, questions=(), proposals=()) -> int:
    rid = reports.save(kit.store, agent,
                       {"status": "warn", "summary": f"{agent} summary",
                        "findings": [{"title": "f", "severity": "warn"}],
                        "questions": list(questions), "proposals": list(proposals)})
    kit.store.execute("UPDATE ck_reports SET created=? WHERE id=?", (created, rid))
    return rid


@pytest.fixture()
def fleet(tmp_path):
    """Two projects: 'alpha' (older report) and 'beta' (newer), each with open items."""
    a = _make_project(tmp_path, "alpha")
    b = _make_project(tmp_path, "beta")
    _seed(a, "selfcheck", created=1000, questions=["alpha q"], proposals=["alpha p"])
    _seed(b, "selfcheck", created=2000, questions=["beta q"])
    f = Fleet([
        Project(key="alpha", label="Alpha", db=a.cfg.db_path, color="#f00", url="/alpha/"),
        Project(key="beta", label="Beta", db=b.cfg.db_path),
    ])
    f.kits = {"alpha": a, "beta": b}          # test convenience, not part of the API
    return f


# ------------------------------------------------------------------ project list

def test_project_requires_absolute_db_path():
    with pytest.raises(ValueError):
        Project.from_dict({"key": "x", "db": "data/app.db"})


def test_project_key_may_not_contain_the_separator():
    with pytest.raises(ValueError):
        Project.from_dict({"key": "a:b", "db": "/tmp/x.db"})


def test_project_needs_a_key_and_a_db():
    with pytest.raises(ValueError):
        Project.from_dict({"db": "/tmp/x.db"})
    with pytest.raises(ValueError):
        Project.from_dict({"key": "x"})


def test_duplicate_keys_are_refused(tmp_path):
    p = Project(key="x", label="X", db=tmp_path / "x.db")
    with pytest.raises(ValueError):
        Fleet([p, p])


def test_from_json_reads_a_bare_list_and_an_object(tmp_path):
    entries = [{"key": "a", "label": "A", "db": str(tmp_path / "a.db")}]
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(entries))
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"projects": entries}))
    assert [p.key for p in Fleet.from_json(bare).all] == ["a"]
    assert [p.key for p in Fleet.from_json(wrapped).all] == ["a"]


def test_disabled_project_is_hidden(tmp_path, fleet):
    f = Fleet([Project(key="a", label="A", db=tmp_path / "a.db", enabled=False)])
    assert f.all == []
    with pytest.raises(UnknownProject):
        f.get("a")


def test_reload_picks_up_a_new_project(tmp_path, fleet):
    path = tmp_path / "projects.json"
    entries = [{"key": "alpha", "label": "Alpha", "db": str(fleet.get("alpha").db)}]
    path.write_text(json.dumps(entries))
    f = Fleet.from_json(path)
    assert [p.key for p in f.all] == ["alpha"]
    entries.append({"key": "beta", "label": "Beta", "db": str(fleet.get("beta").db)})
    path.write_text(json.dumps(entries))
    assert [p.key for p in f.reload().all] == ["alpha", "beta"]


# ------------------------------------------------------------------ fleet ids

def test_fleet_id_roundtrip():
    assert split_id(make_id("homeflix", 23)) == ("homeflix", 23)


@pytest.mark.parametrize("bad", ["23", "homeflix:", ":23", "homeflix:abc", ""])
def test_bad_fleet_id_is_rejected(bad):
    with pytest.raises(ValueError):
        split_id(bad)


def test_a_key_containing_a_dash_still_splits():
    assert split_id("real-estate:7") == ("real-estate", 7)


# ------------------------------------------------------------------ reading

def test_reports_merge_newest_first_and_carry_their_project(fleet):
    rows = fleet.reports()
    assert [r["project"] for r in rows] == ["beta", "alpha"]
    assert rows[0]["id"] == "beta:1" and rows[0]["local_id"] == 1
    assert rows[1]["project_label"] == "Alpha" and rows[1]["project_color"] == "#f00"
    # findings survive the round trip through claudekit.reports
    assert rows[0]["findings"][0]["title"] == "f"


def test_limit_applies_to_the_merged_list(fleet):
    assert len(fleet.reports(limit=1)) == 1


def test_reports_can_be_filtered_to_one_project(fleet):
    assert {r["project"] for r in fleet.reports(project="alpha")} == {"alpha"}


def test_open_items_merge_and_are_addressable(fleet):
    items = fleet.open_items()
    assert {i["project"] for i in items} == {"alpha", "beta"}
    assert all(split_id(i["id"])[0] == i["project"] for i in items)
    # the parent report is a fleet id too, or a client could not navigate to it
    assert all(split_id(i["report_id"])[0] == i["project"] for i in items)


def test_report_detail_tags_its_items(fleet):
    rep = fleet.report("alpha:1")
    assert rep["project"] == "alpha" and rep["open_items"] == 2
    assert sorted(i["id"] for i in rep["items"]) == ["alpha:1", "alpha:2"]


def test_report_from_an_unknown_project_is_a_lookup_error(fleet):
    with pytest.raises(UnknownProject):
        fleet.report("nope:1")


def test_missing_report_is_none_not_an_error(fleet):
    assert fleet.report("alpha:999") is None


def test_status_counts_each_project(fleet):
    by_key = {r["key"]: r for r in fleet.status()}
    assert by_key["alpha"]["reports"] == 1 and by_key["alpha"]["open_items"] == 2
    assert by_key["beta"]["open_items"] == 1
    assert all(r["ok"] for r in by_key.values())


def test_counts_sum_the_fleet(fleet):
    assert fleet.counts() == {"projects": 2, "degraded": 0, "reports": 2,
                              "open_items": 3, "pending_action": 0}


# ------------------------------------------------------------------ degradation

def test_a_missing_database_degrades_rather_than_crashing(tmp_path, fleet):
    broken = Project(key="gone", label="Gone", db=tmp_path / "nope" / "missing.db")
    f = Fleet([*fleet.all, broken])
    row = {r["key"]: r for r in f.status()}["gone"]
    assert row["ok"] is False and row["error"]
    # …and the healthy projects still list
    assert len(f.reports()) == 2
    assert f.counts()["degraded"] == 1


def test_listing_never_creates_a_missing_database(tmp_path):
    path = tmp_path / "absent.db"
    f = Fleet([Project(key="x", label="X", db=path)])
    f.status(), f.reports(), f.open_items()
    assert not path.exists()


def test_writing_to_an_unreachable_project_is_an_error(tmp_path):
    f = Fleet([Project(key="x", label="X", db=tmp_path / "absent.db")])
    with pytest.raises(ProjectUnavailable):
        f.answer("x:1", "hi")


# ------------------------------------------------------------------ writing

def test_answer_lands_in_the_right_project(fleet):
    it = fleet.answer("alpha:1", "because")
    assert it["status"] == "answered" and it["answer"] == "because"
    assert it["project"] == "alpha" and it["id"] == "alpha:1"
    # the other project's item of the same local id is untouched
    assert reports.item(fleet.kits["beta"].store, 1)["status"] == "open"


def test_decide_approves_and_rejects_in_place(fleet):
    assert fleet.decide("alpha:2", True, "go on")["status"] == "approved"
    assert reports.item(fleet.kits["alpha"].store, 2)["answer"] == "go on"
    fleet.decide("alpha:2", False)
    assert reports.item(fleet.kits["alpha"].store, 2)["status"] == "rejected"


def test_answering_an_unknown_item_is_none(fleet):
    assert fleet.answer("alpha:999", "x") is None


def test_answering_requests_the_projects_apply_run(fleet):
    it = fleet.answer("beta:1", "yes")
    assert it["applying"] == "applydecisions"
    row = fleet.kits["beta"].store.one(
        "SELECT run_requested FROM ck_schedule_task WHERE task='applydecisions'")
    assert row and row["run_requested"] is not None


def test_rejecting_does_not_wake_the_projects_apply_agent(fleet):
    it = fleet.decide("alpha:2", False, "no thanks")
    assert it["status"] == "rejected" and it["applying"] is None
    assert not fleet.kits["alpha"].store.query(
        "SELECT 1 FROM ck_schedule_task WHERE run_requested IS NOT NULL")


def test_a_decision_goes_to_the_task_its_agent_declares(tmp_path):
    kit = _make_project(tmp_path, "gamma", agents=("sldubs",))
    _seed(kit, "sldubs", created=1000, proposals=["do the thing"])
    f = Fleet([Project(key="gamma", label="Gamma", db=kit.cfg.db_path,
                       decisions_tasks={"sldubs": "sldubsmaint"})])
    assert f.decide("gamma:1", True)["applying"] == "sldubsmaint"
    assert kit.store.one("SELECT 1 FROM ck_schedule_task WHERE task='sldubsmaint'")


# ------------------------------------------------------------------ apply

def test_apply_queues_a_run_per_project_with_outstanding_items(fleet):
    fleet.answer("alpha:1", "a")
    fleet.decide("beta:1", True)
    res = fleet.apply()
    assert res["ok"] and not res["failed"]
    assert {q["project"] for q in res["queued"]} == {"alpha", "beta"}
    assert all(q["task"] == "applydecisions" for q in res["queued"])
    assert "Queued 2 items" in res["note"]


def test_apply_with_nothing_decided_is_skipped(fleet):
    res = fleet.apply()
    assert res["queued"] == [] and res["skipped"]


def test_apply_can_target_one_project(fleet):
    fleet.answer("alpha:1", "a")
    fleet.decide("beta:1", True)
    res = fleet.apply(project="beta")
    assert [q["project"] for q in res["queued"]] == ["beta"]


def test_apply_reports_a_broken_project_rather_than_failing(tmp_path, fleet):
    fleet.answer("alpha:1", "a")
    f = Fleet([*fleet.all, Project(key="gone", label="Gone", db=tmp_path / "absent.db")])
    res = f.apply()
    assert [q["project"] for q in res["queued"]] == ["alpha"]
    assert [x["project"] for x in res["failed"]] == ["gone"]


def test_actionable_is_merged_and_tagged(fleet):
    fleet.answer("alpha:1", "a")
    rows = fleet.actionable()
    assert [r["id"] for r in rows] == ["alpha:1"]


# ------------------------------------------------------------------ http adapter

@pytest.fixture()
def client(fleet):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from claudekit.http.fleet_router import build_router

    app = fastapi.FastAPI()
    app.include_router(build_router(fleet, prefix="/api/fleet"))
    return TestClient(app)


def test_http_lists_projects_and_reports(client):
    assert {p["key"] for p in client.get("/api/fleet/projects").json()} == {"alpha", "beta"}
    rows = client.get("/api/fleet/reports?limit=30").json()
    assert [r["id"] for r in rows] == ["beta:1", "alpha:1"]


def test_http_open_is_not_swallowed_as_a_report_id(client):
    r = client.get("/api/fleet/reports/open")
    assert r.status_code == 200 and len(r.json()) == 3


def test_http_report_detail_and_404s(client):
    assert client.get("/api/fleet/reports/alpha:1").json()["project"] == "alpha"
    assert client.get("/api/fleet/reports/alpha:999").status_code == 404
    assert client.get("/api/fleet/reports/nope:1").status_code == 404
    assert client.get("/api/fleet/reports/rubbish").status_code == 400


def test_http_answer_and_decide_roundtrip(client, fleet):
    r = client.post("/api/fleet/reports/item/alpha:1/answer", json={"answer": "sure"})
    assert r.status_code == 200 and r.json()["status"] == "answered"
    r = client.post("/api/fleet/reports/item/alpha:2/decide",
                    json={"approved": True, "note": "ok"})
    assert r.json()["status"] == "approved"
    assert reports.item(fleet.kits["alpha"].store, 2)["status"] == "approved"


def test_http_apply_returns_what_it_queued(client):
    client.post("/api/fleet/reports/item/alpha:1/answer", json={"answer": "sure"})
    body = client.post("/api/fleet/reports/apply").json()
    assert body["ok"] and body["queued"][0]["project"] == "alpha"


def test_http_health_carries_the_counts(client):
    body = client.get("/api/fleet/health").json()
    assert body["ok"] and body["projects"] == 2 and body["reports"] == 2


def test_http_unreachable_project_is_a_bad_gateway(tmp_path, fleet):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from claudekit.http.fleet_router import build_router

    f = Fleet([Project(key="gone", label="Gone", db=tmp_path / "absent.db")])
    app = fastapi.FastAPI()
    app.include_router(build_router(f))
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/fleet/reports/gone:1").status_code == 502

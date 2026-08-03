"""Agent registry: code-defined defaults, database-stored user overrides.

The host declares its agents as `AgentSpec`s in `KitConfig.agents`. The UI can edit an agent's
system/user prompt and its daily hour; those overrides live in `ck_agent_config` and win over the
defaults. Resetting an agent just deletes its override row.
"""

from __future__ import annotations

import time

from .config import AgentSpec, KitConfig
from .store import Store


def _override(store: Store, name: str):
    return store.one("SELECT * FROM ck_agent_config WHERE agent=?", (name,))


def resolve(cfg: KitConfig, store: Store, name: str) -> AgentSpec:
    """The effective spec for `name`: defaults with any stored override applied."""
    base = cfg.agents.get(name)
    if base is None:
        raise KeyError(f"unknown agent: {name}")
    row = _override(store, name)
    if not row:
        return base
    return AgentSpec(
        name=base.name,
        label=base.label,
        description=base.description,
        system=row["system"] if row["system"] else base.system,
        prompt=row["prompt"] if row["prompt"] else base.prompt,
        schedule=base.schedule,
        model=base.model,
        script=base.script,
        add_dirs=base.add_dirs,
    )


def set_prompt(store: Store, name: str, system: str | None, prompt: str | None) -> None:
    row = _override(store, name)
    if row:
        store.execute(
            "UPDATE ck_agent_config SET system=?, prompt=?, updated=? WHERE agent=?",
            (system, prompt, time.time(), name))
    else:
        store.execute(
            "INSERT INTO ck_agent_config (agent, system, prompt, updated) VALUES (?,?,?,?)",
            (name, system, prompt, time.time()))


def reset(store: Store, name: str) -> None:
    store.execute("DELETE FROM ck_agent_config WHERE agent=?", (name,))


def get_hour(cfg: KitConfig, store: Store, name: str, default: int = 4) -> int:
    row = _override(store, name)
    if row and row["hour"] is not None:
        return int(row["hour"])
    return default


def set_hour(store: Store, name: str, hour: int) -> int:
    hour = max(0, min(23, int(hour)))
    row = _override(store, name)
    if row:
        store.execute("UPDATE ck_agent_config SET hour=?, updated=? WHERE agent=?",
                      (hour, time.time(), name))
    else:
        store.execute("INSERT INTO ck_agent_config (agent, hour, updated) VALUES (?,?,?)",
                      (name, hour, time.time()))
    return hour


def describe(cfg: KitConfig, store: Store) -> list[dict]:
    """Everything the Settings page needs to render the agent list."""
    out = []
    for name, base in cfg.agents.items():
        eff = resolve(cfg, store, name)
        row = _override(store, name)
        out.append({
            "name": name,
            "label": base.label,
            "description": base.description,
            "schedule": base.schedule,
            "model": base.model,
            "system": eff.system,
            "prompt": eff.prompt,
            "customized": bool(row and (row["system"] or row["prompt"])),
            "hour": get_hour(cfg, store, name),
        })
    return out

"""Agent registry: code-defined defaults, database-stored user overrides.

The host declares its agents as `AgentSpec`s in `KitConfig.agents`. The UI can edit an agent's
system/user prompt and its daily hour; those overrides live in `ck_agent_config` and win over the
defaults. Resetting an agent just deletes its override row.
"""

from __future__ import annotations

import time

from dataclasses import replace

from .config import AgentSpec, KitConfig
from .store import Store


def _override(store: Store, name: str):
    return store.one("SELECT * FROM ck_agent_config WHERE agent=?", (name,))


def apply_hints(cfg: KitConfig, spec: AgentSpec) -> AgentSpec:
    """Append `KitConfig.system_hints` not already present in the agent's system prompt.

    Applied here rather than baked into each AgentSpec so a host-wide rule also reaches prompts the
    user has edited in the UI — otherwise customising one agent silently drops it. Idempotent: a
    hint already in the text (from a previous append, or written by hand) is not added twice.
    """
    if spec.no_hints or not cfg.system_hints:
        return spec
    extra = [h for h in cfg.system_hints if h and h not in (spec.system or "")]
    if not extra:
        return spec
    return replace(spec, system=(spec.system or "") + "".join(extra))


def resolve(cfg: KitConfig, store: Store, name: str) -> AgentSpec:
    """The effective spec for `name`: defaults, any stored override applied, then shared hints."""
    base = cfg.agents.get(name)
    if base is None:
        raise KeyError(f"unknown agent: {name}")
    row = _override(store, name)
    if not row:
        return apply_hints(cfg, base)
    return apply_hints(cfg, replace(
        base,
        system=row["system"] if row["system"] else base.system,
        prompt=row["prompt"] if row["prompt"] else base.prompt,
    ))


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

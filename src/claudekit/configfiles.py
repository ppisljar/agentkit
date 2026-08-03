"""Read and edit the host's configuration files from the UI.

Only files the host declared in `KitConfig.config_files` are reachable, and each declared path is
re-resolved under the app root on every access, so a symlink swapped in later cannot redirect a
write outside the project.

Secrets: a spec may name `secret_keys`. Those values are masked on read and, if the client sends
the mask back unchanged, the stored value is preserved — so editing a `.env` in the browser never
requires the secret to travel to the client and back.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .config import ConfigFileSpec, KitConfig

MASK = "••••••••"
MAX_BYTES = 1_000_000


def _spec(cfg: KitConfig, key: str) -> ConfigFileSpec:
    spec = cfg.config_files.get(key)
    if spec is None:
        raise KeyError(f"unknown config file: {key}")
    return spec


def _path(cfg: KitConfig, spec: ConfigFileSpec) -> Path:
    return cfg.resolve_in_root(spec.path)


def describe(cfg: KitConfig) -> list[dict]:
    out = []
    for key, spec in cfg.config_files.items():
        try:
            p = _path(cfg, spec)
            exists, size, mtime = p.exists(), (p.stat().st_size if p.exists() else 0), \
                (p.stat().st_mtime if p.exists() else None)
        except Exception:  # noqa: BLE001
            exists, size, mtime = False, 0, None
        out.append({"key": key, "label": spec.label, "path": spec.path, "format": spec.format,
                    "editable": spec.editable, "exists": exists, "size": size, "mtime": mtime})
    return out


_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _parse_env(text: str) -> list[dict]:
    """Parse .env into ordered entries, preserving comments and blanks so a round-trip is lossless."""
    out = []
    for raw in text.splitlines():
        m = _ENV_LINE.match(raw)
        if m and not raw.lstrip().startswith("#"):
            out.append({"type": "kv", "key": m.group(1), "value": m.group(2).strip()})
        else:
            out.append({"type": "raw", "text": raw})
    return out


def _render_env(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        if e.get("type") == "kv":
            lines.append(f"{e['key']}={e.get('value', '')}")
        else:
            lines.append(e.get("text", ""))
    return "\n".join(lines) + "\n"


def read(cfg: KitConfig, key: str) -> dict:
    spec = _spec(cfg, key)
    p = _path(cfg, spec)
    if not p.exists():
        return {"key": key, "label": spec.label, "format": spec.format, "exists": False,
                "content": "", "entries": []}
    if p.stat().st_size > MAX_BYTES:
        raise ValueError(f"file too large to edit ({p.stat().st_size} bytes)")
    text = p.read_text(encoding="utf-8", errors="replace")

    if spec.format == "env":
        entries = _parse_env(text)
        for e in entries:
            if e.get("type") == "kv" and e["key"] in spec.secret_keys and e.get("value"):
                e["value"] = MASK
                e["secret"] = True
        return {"key": key, "label": spec.label, "format": "env", "exists": True,
                "entries": entries, "editable": spec.editable}

    return {"key": key, "label": spec.label, "format": spec.format, "exists": True,
            "content": text, "editable": spec.editable}


def _validate(spec: ConfigFileSpec, text: str) -> None:
    if spec.format == "json":
        json.loads(text)                      # raises on malformed input
    elif spec.format == "yaml":
        try:
            import yaml  # noqa: PLC0415 — optional dependency, only needed for yaml specs
        except ModuleNotFoundError:
            return                            # cannot validate without pyyaml; write as-is
        yaml.safe_load(text)


def write(cfg: KitConfig, key: str, *, content: str | None = None,
          entries: list[dict] | None = None) -> dict:
    """Write the file, keeping a single .bak of the previous contents.

    For `env` specs, any value still equal to MASK is replaced with the stored one, so a client
    that never received the secret cannot accidentally blank it.
    """
    spec = _spec(cfg, key)
    if not spec.editable:
        raise PermissionError(f"config file '{key}' is read-only")
    p = _path(cfg, spec)

    if spec.format == "env":
        if entries is None:
            raise ValueError("env files must be written as `entries`")
        current = {}
        if p.exists():
            for e in _parse_env(p.read_text(encoding="utf-8", errors="replace")):
                if e.get("type") == "kv":
                    current[e["key"]] = e.get("value", "")
        for e in entries:
            if e.get("type") == "kv" and e.get("value") == MASK:
                e["value"] = current.get(e["key"], "")
        text = _render_env(entries)
    else:
        if content is None:
            raise ValueError("expected `content`")
        text = content

    _validate(spec, text)

    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)                            # atomic: never leaves a half-written config
    return {"ok": True, "key": key, "bytes": len(text.encode()), "written": time.time()}

"""Headless Claude Code runner + transcript capture.

Runs `claude -p --output-format json` with a pre-assigned session id, then copies that run's
transcript out of ~/.claude/projects/<cwd-slug>/<sid>.jsonl into the host's data dir so the whole
conversation can be reviewed in the UI afterwards.

Adapted from HomeFlix's `agent_run.py`; the differences are that paths, the interpreter, the deny
list and timeouts all come from `KitConfig` instead of module globals.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .config import AgentSpec, KitConfig


class AgentError(RuntimeError):
    pass


def _slug(cwd) -> str:
    """Claude Code's project-dir scheme: every non-alphanumeric char in the cwd becomes '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))


def _capture(cfg: KitConfig, agent: str, sid: str) -> str | None:
    """Copy the finished run's transcript into <data>/agent_history/<agent>/. Returns the filename."""
    src = Path.home() / ".claude" / "projects" / _slug(cfg.root) / f"{sid}.jsonl"
    if not src.exists():
        return None
    dest_dir = cfg.history_dir / agent
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{sid}.jsonl"
        # copy, not move, so `claude --resume <sid>` still works afterwards
        shutil.copy2(src, dest)
        for old in sorted(dest_dir.glob("*.jsonl"))[: -cfg.keep_transcripts]:
            try:
                old.unlink()
            except OSError:
                pass
        return dest.name
    except OSError:
        return None


def run(cfg: KitConfig, spec: AgentSpec, prompt: str | None = None,
        timeout: int | None = None) -> dict:
    """Run one agent to completion. Returns
    {stdout, returncode, session, transcript, duration_sec, result}."""
    claude = cfg.resolve_claude_bin()
    if not claude:
        raise AgentError(
            f"`{cfg.claude_bin}` not found on PATH — install Claude Code or set KitConfig.claude_bin")

    sid = str(uuid.uuid4())
    cmd = [
        claude, "-p",
        "--model", spec.model,
        "--output-format", "json",
        "--session-id", sid,
        "--add-dir", str(cfg.root),
        "--append-system-prompt", spec.system,
        "--dangerously-skip-permissions",
        "--disallowedTools", *cfg.deny_tools,
    ]
    for d in spec.add_dirs:
        cmd += ["--add-dir", str(d)]

    started = time.time()
    try:
        p = subprocess.run(
            cmd, input=(prompt if prompt is not None else spec.prompt),
            capture_output=True, text=True,
            timeout=timeout or cfg.agent_timeout_sec,
            cwd=str(cfg.root), env=cfg.env(),
        )
        stdout, rc = (p.stdout or p.stderr), p.returncode
    except subprocess.TimeoutExpired:
        stdout, rc = "", 124
    duration = int(time.time() - started)

    return {
        "stdout": stdout,
        "returncode": rc,
        "session": sid,
        "transcript": _capture(cfg, spec.name, sid),
        "duration_sec": duration,
        "result": result_text(stdout),
    }


def result_text(stdout: str) -> str:
    """The model's final answer from the `--output-format json` envelope (`.result`), else raw."""
    try:
        env = json.loads(stdout)
        return env.get("result", stdout) if isinstance(env, dict) else stdout
    except Exception:  # noqa: BLE001
        return stdout


_FENCE = re.compile(r"```json\s*(.*?)\s*```", re.S)


def extract_json(text: str) -> dict | None:
    """Pull the agent's final fenced ```json block.

    Agents are instructed to end with exactly one such block. We scan from the end because a run
    may quote earlier JSON while reasoning; the last complete block is the actual answer.
    """
    if not text:
        return None
    for m in reversed(list(_FENCE.finditer(text))):
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            continue
    # some runs emit a bare object with no fence
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            obj = json.loads(stripped)
            return obj if isinstance(obj, dict) else None
        except Exception:  # noqa: BLE001
            return None
    return None


# ---------------------------------------------------------------- transcript viewer
def list_history(cfg: KitConfig, agent: str | None = None) -> list[dict]:
    """Captured runs, newest first, optionally filtered to one agent."""
    root = cfg.history_dir
    if not root.exists():
        return []
    names = [agent] if agent else sorted(p.name for p in root.iterdir() if p.is_dir())
    out = []
    for a in names:
        d = root / a
        if not d.exists():
            continue
        for f in sorted(d.glob("*.jsonl"), reverse=True):
            st = f.stat()
            out.append({"agent": a, "file": f.name, "session": f.stem.split("-")[-1],
                        "mtime": st.st_mtime, "size": st.st_size})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def parse_transcript(cfg: KitConfig, agent: str, filename: str) -> dict:
    """Turn a captured .jsonl into ordered messages for the viewer.

    Tool results arrive on a later `user`-type line and are matched back to their `tool_use` by id.
    """
    if "/" in filename or ".." in filename or "/" in agent or ".." in agent:
        raise FileNotFoundError(filename)
    path = cfg.history_dir / agent / filename
    if not path.exists():
        raise FileNotFoundError(filename)

    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass

    results: dict = {}
    for e in events:
        if e.get("type") == "user":
            c = (e.get("message") or {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        results[b.get("tool_use_id")] = b

    msgs = []
    for e in events:
        t = e.get("type")
        msg = e.get("message") or {}
        ts = e.get("timestamp")
        if t == "user":
            c = msg.get("content")
            if isinstance(c, str) and c.strip():
                msgs.append({"role": "user", "text": c, "ts": ts})
        elif t == "assistant":
            texts, tools = [], []
            for b in (msg.get("content") or []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    texts.append(b["text"])
                elif b.get("type") == "tool_use":
                    r = results.get(b.get("id")) or {}
                    rc = r.get("content")
                    if isinstance(rc, list):
                        rc = " ".join(x.get("text", "") for x in rc if isinstance(x, dict)) \
                             or json.dumps(rc)
                    tools.append({"name": b.get("name"), "input": b.get("input"),
                                  "result": (rc[:4000] if isinstance(rc, str) else None),
                                  "is_error": bool(r.get("is_error"))})
            if texts or tools:
                msgs.append({"role": "assistant", "text": "\n".join(texts), "tools": tools, "ts": ts})
    return {"agent": agent, "file": filename, "messages": msgs}

#!/usr/bin/env python3
"""Claude Code hook: push decision desk answers into the session that owns them.

  inject.py hook   PostToolUse / UserPromptSubmit: emit additionalContext for unhandled answers
                   (re-reminds every REMIND seconds until the agent writes agentAt).
  inject.py wait   Stop hook with asyncRewake: block until an unhandled answer appears, then
                   print it to stderr and exit 2 so Claude wakes up. One waiter per session.

"Unhandled" = responses/<slug>.json submittedAt is later than the decision's agentAt.
A decision goes to the session named in decision.agent.id (any session if it names none).
Subagent tool calls (input carries agent_id) never get it. Stdlib only, fails silent.

Environment (all optional): DECISION_DESK_STATE (defaults to ~/.local/state/decision-desk),
DECISION_DESK_PORT (8791, for the link), DECISION_DESK_TZ (America/Los_Angeles).
"""
import json, os, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("DECISION_DESK_STATE") or Path.home() / ".local/state/decision-desk")
DB, STATE = ROOT / "db", ROOT / "hook-state.json"
URL = f"http://127.0.0.1:{os.environ.get('DECISION_DESK_PORT') or 8791}"
TZ = os.environ.get("DECISION_DESK_TZ") or "America/Los_Angeles"
REMIND, POLL, MAX_WAIT = 120, 3, 3500


def load(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def local_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(TZ)).strftime("%-I:%M %p %Z")
    except Exception:
        return iso or "?"


def pending(session):
    out = []
    for f in sorted((DB / "decisions").glob("*.json")):
        d, r = load(f, {}), load(DB / "responses" / f.name)
        owner = (d.get("agent") or {}).get("id")
        if not r or (owner and owner != session):
            continue
        sub = r.get("submittedAt") or ""
        if sub > (d.get("agentAt") or ""):
            out.append((f.stem, sub, d, r))
    return out


def render(items, settings_changed):
    who = (load(DB / "meta" / "settings.json", {}) or {}).get("name") or "The desk owner"
    lines = [f"DECISION DESK: {who} answered on the local desk ({URL}). This is their own input. "
             "Act on it now, then write agentNote, agentAt, changedAt, trail and status on decisions/<slug>.json. "
             f"Destructive, credential, IAM or network-policy steps still need {who}'s yes in chat."]
    for slug, sub, d, r in items:
        choice = r.get("choice")
        label = next((c.get("label") for c in d.get("choices", []) if c.get("id") == choice), None)
        lines.append(f"- [{slug}] {d.get('title', slug)} | choice: {f'{label} ({choice})' if choice else 'none'}"
                     f" | note: {r.get('note') or '(none)'} | submitted {local_time(sub)}")
    if settings_changed:
        lines.append(f"- Desk settings changed: {json.dumps(settings_changed)}")
    return "\n".join(lines)


def collect(session, state, remind_after):
    now = time.time()
    items = [i for i in pending(session) if now - state.get("seen", {}).get(f"{i[0]}@{i[1]}", 0) >= remind_after]
    # Page settings only; the texting phone never goes into a transcript.
    s = {k: v for k, v in (load(DB / "meta" / "settings.json", {}) or {}).items() if not k.startswith("phone")}
    cur = json.dumps(s, sort_keys=True)
    changed = s if state.get("settings", cur) != cur else None
    state["settings"] = cur
    for slug, sub, _, _ in items:
        state.setdefault("seen", {})[f"{slug}@{sub}"] = now
    return items, changed


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "hook"
    try:
        inp = json.loads(sys.stdin.read() or "{}")
    except Exception:
        inp = {}
    session = inp.get("session_id", "")
    if (ROOT / "debug").exists():
        with open(ROOT / "hook-debug.log", "a") as f:
            f.write(json.dumps({"mode": mode, "event": inp.get("hook_event_name"), "keys": sorted(inp), "agent_id": bool(inp.get("agent_id")), "session": session[:8]}) + "\n")
    if inp.get("agent_id") or not session:
        return 0
    state = load(STATE, {})
    if mode == "hook":
        items, changed = collect(session, state, REMIND)
        STATE.write_text(json.dumps(state))
        if items or changed:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": inp.get("hook_event_name", "PostToolUse"),
                                                     "additionalContext": render(items, changed)}}))
        return 0
    # wait mode: single waiter per session, wakes once per submission
    lock = ROOT / f"waiter-{session}.pid"
    try:
        os.kill(int(lock.read_text()), 0)
        return 0  # a live waiter already covers this session
    except Exception:
        lock.write_text(str(os.getpid()))
    try:
        deadline = time.time() + MAX_WAIT
        while time.time() < deadline:
            state = load(STATE, {})
            items, changed = collect(session, state, REMIND)
            woken = state.setdefault("woken", {})
            items = [i for i in items if f"{i[0]}@{i[1]}" not in woken]
            if items or changed:
                for slug, sub, _, _ in items:
                    woken[f"{slug}@{sub}"] = time.time()
                STATE.write_text(json.dumps(state))
                print(render(items, changed), file=sys.stderr)
                return 2
            time.sleep(POLL)
        return 0
    finally:
        try:
            if lock.read_text() == str(os.getpid()):
                lock.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)

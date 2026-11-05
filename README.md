# Decision desk — setup for your own Claude Code agent

This is a private claude.ai page where your agent lists the decisions waiting on you, and where you answer them. Each decision shows what's being decided, the background, the deadline, what changes under each option, the risks and the sources the agent checked. You pick an option, tap "Need more info", or reply, then send. Your agent reads the answer, acts on it and updates the decision's status: Sent, In progress, Waiting on others, Your move or Done.

Your agent has to publish its own copy, so the page belongs to you. Only the page's owner can submit answers.

## Setup (give this file to your Claude Code agent)

1. Publish `index.html` as a new artifact with the Artifact tool. Pass `capabilities: {db: {rules: [{path: "", read: "view", write: "owner"}]}}` and icon `checklist`.
2. Write `meta/settings` with ArtifactData:
   `{name: "<your first name>", title: "Decisions for <name>", cutoffAt: "2026-11-09T07:59:00Z", onsiteAt: "2026-11-15T16:00:00Z"}`
3. Schedule a recurring check every 3 minutes that runs this prompt:
   > Read the "decisions" collection on <url>, query status == "answered". For each, read its doc in "responses", act on the answer within my authorization, and write `agentNote`/`agentAt`. Set status to "done", "waiting" or "followup", whichever is true. A note starting "Need more info" means research it again and rewrite the decision, then set it back to "open". Anything destructive, credential-related, IAM or network-policy needs my explicit confirmation first.
4. Whenever something needs your call, the agent adds a decision instead of asking in chat.

## Run it locally (no artifact)

You can also run the page on your own machine, with no claude.ai artifact. `local/server.py` serves `index.html` at http://127.0.0.1:8791 and gives it the same data store, backed by JSON files. Only your machine can reach it. It needs Python 3.9 or later and nothing else.

1. Start it: `python3 local/server.py`. To keep it running in the background: `nohup python3 local/server.py >~/.local/state/decision-desk/server.log 2>&1 &`
2. Open http://127.0.0.1:8791.
3. The data lives in `~/.local/state/decision-desk/db/<collection>/<id>.json`, one JSON object per doc, in the collections of the data contract below. Your agent reads and writes these files directly. Write atomically: write `<id>.json.tmp`, then rename it over `<id>.json`.

Settings are environment variables, all optional: `DECISION_DESK_PORT` (8791), `DECISION_DESK_STATE` (`~/.local/state/decision-desk`), `DECISION_DESK_DATA` (`<state>/db`), `DECISION_DESK_PAGE` (this repo's `index.html`) and `DECISION_DESK_TZ` (`America/Los_Angeles`, for times in hook messages). To check it all works: `python3 -m unittest local/test_local.py`.

### Answers straight into Claude Code

`local/inject.py` is a Claude Code hook that hands each answer to the session that owns the decision, so you don't need the 3-minute check. Add this to `~/.claude/settings.json`, with the path to your clone:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "python3 \"$HOME/src/decision-desk/local/inject.py\" hook", "timeout": 5 }] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "python3 \"$HOME/src/decision-desk/local/inject.py\" hook", "timeout": 5 }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "python3 \"$HOME/src/decision-desk/local/inject.py\" wait", "asyncRewake": true, "timeout": 3600 }] }
    ]
  }
}
```

How answers find their session:
- When the agent adds a decision, it sets `agent: {kind: "session", id: <its session id>}`. Answers to that decision go only to that session. A decision with no `agent` goes to any session.
- After each tool call and each prompt you send, the hook adds unhandled answers to the agent's context. An answer stays unhandled while its `submittedAt` is later than the decision's `agentAt`, so the agent writes `agentAt` once it has acted. Until then the hook reminds it every 2 minutes.
- When the session is idle, the Stop hook waits in the background and wakes it once per answer.
- Subagents never get answers. Only the session that owns the decision does.

## Text-message line (optional)

The local server can text you the decisions that need you, and you answer by replying. It uses a [Photon Spectrum](https://photon.codes/docs/spectrum-ts/getting-started) iMessage line.

How it works:
- In the page, tap Text me, enter your number and tap Connect. The line texts you a 6-digit code. Enter it and tap Verify. Your number is saved in `meta/settings` as `phone`, with `phoneVerified: true`.
- Each open decision is texted to you once: its tag, the title and the numbered choices, with the recommended one marked. If the agent rewrites it (a new `changedAt`) and it's open again, you get it again.
- Reply `2`, or `2 sounds good` to add a note. When several are open, start with the tag: `<tag> 2 sounds good`. Text `list` to get all the open ones again. A reply the desk can't match gets a short help text back.
- A texted answer is saved exactly like one sent from the page (`responses/<slug>`, then status `answered`), so the hooks deliver it the same way.
- Only your verified number is heard. Texts from anyone else are dropped without a reply.

Setup:
1. Spectrum's SDK is TypeScript only, so a small Node script, `local/spectrum-bridge.mjs`, does the sending and receiving. Install Node 18.17 or later, then run `npm install` in `local/`. The server starts the script when it's needed and talks to it over a pipe; it opens no ports.
2. Copy `local/spectrum.env.example` to `~/.local/state/decision-desk/spectrum.env`, run `chmod 600` on it, and fill in your project id, project secret and the line's number. Keep it out of git.
3. Restart the server. It prints `texting=on` when everything is in place.

Things to know:
- Replies come in over the SDK's own connection, so there is no webhook and nothing open to the internet.
- Use a line nothing else answers. Every webhook registered on a Spectrum project gets the texts sent to that project's lines, so if another app shares the project, it gets your replies too.
- Photon's deliverability guide prefers that people text the line first. The desk texts first (the code), so Apple may show a "Report Junk" banner until you save the line as a contact.

## Data contract

`decisions/<slug>` (all optional except title, choices, status, order):
- `title`: the question
- `what`: one line on what's being decided
- `background`: [bullets]
- `when`: the deadline in words
- `due`: ISO date-time, which drives sorting and the urgency color
- `area`: Install, Memory, Security, Ops, Admin, Release or Comms
- `changes`: [{choice, effect}]
- `ifNoDecision`
- `choices`: [{id, label, why}]. `why` shows as "Why" on the recommended choice and "Why not" on the others
- `recommended`: a choice id
- `risks`: [strings]. An empty list shows "None known"
- `checked`: [{source, at, note}], the sources the agent re-read before writing it
- `tickets`: [{id, status, url}], live ticket chips
- `links`: [{label, url}]
- `command`: a shell block Ben runs himself; shown with a Copy button. A `background` or `points` bullet starting "Command: " is shown the same way, and is skipped when `command` is set
- `status`: open, answered, waiting, followup or done
- `agentNote`, `agentAt`, `changedAt`, `ticketNote` {label, url}
- `order`, `createdAt`
- `agent`: {kind: "session" | "subagent", id, label, resume?, transcript?}, the session or subagent that owns the decision. The page shows "Owned by <label>" with the short id, and Copy copies `resume` if present, otherwise the id
- `handlers`: [{kind: "subagent", id, label, startedAt, status: "running" | "finished" | "failed", summary?}], the subagents that worked on it (one per answer). The collapsed tile shows a count such as "1 agent running"
- `trail`: [{at, actor, action}], an append-only history shown as "History (N)"

`responses/<same slug>` is written by the page: {choice, note, submittedAt}.

`digest/<area>` is an optional "What Claude did" log: {order, area, headline, period, items: [{text, url}], updatedAt}.

`meta/settings` also holds `phone` and `phoneVerified` once you connect a phone in local mode. The hooks never pass them to the agent.

## Writing rules that made it work for Ben

- Write each decision for someone coming in cold, with no ticket shorthand. Use bullets, not paragraphs.
- Every choice gets a one-line Why or Why not, and every decision gets a Risks section, even if it's "None known".
- Never mark something done while a step remains. Use `followup` for "Your move" and `waiting` for "Waiting on others".
- Use `followup` only when you must act on the desk right now. A step that comes later is `done`, with the step in `agentNote`, and the agent asks you in chat when it's due.
- Re-read Slack posts and threads, GitHub and Linear before writing a decision, and list them in `checked`.

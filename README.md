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

Settings are environment variables, all optional: `DECISION_DESK_PORT` (8791), `DECISION_DESK_STATE` (`~/.local/state/decision-desk`), `DECISION_DESK_DATA` (`<state>/db`), `DECISION_DESK_PAGE` (this repo's `index.html`) and `DECISION_DESK_TZ` (`America/Los_Angeles`, for times in hook messages).

How it stays quick:
- The server tells the page which collections changed, as they change, over one event stream. Nothing polls, and an agent's write shows up in about a tenth of a second.
- Your answers show on the page the moment you send them, the way the artifact's store does it, and the server's copy confirms them. If a save fails, the page puts things back and says so.
- The server keeps parsed files in memory and re-reads one only when it changes.

To check it all works: `python3 -m unittest local/test_local.py`. The browser checks need Google Chrome: run `npm install` in `local/`, then `node --test local/test_browser.mjs`.

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
- In the page, tap Text me, enter your number and tap Connect. The line texts you a 6-digit code. Enter it and tap Verify, and a short hello arrives. Your number is saved in `meta/settings` as `phone`, with `phoneVerified: true`.
- Each decision that needs you is texted once: its tag, the title and the numbered choices, with the recommended one marked. That means open decisions, and "Your move" followups that have choices. If the agent rewrites one (a new `changedAt` or status), you get it again. Texts go out a few seconds apart.
- Reply `2`, or `2 sounds good` to add a note. When several are open, start with the tag: `<tag> 2 sounds good`. The desk texts back what it sent.
- Text `list` to get the open ones again, `help` for these rules, or `stop` to turn texts off. A reply the desk can't match gets a short help text, at most 3 every 10 minutes, so a phone's auto-reply can't start a loop.
- A texted answer is saved exactly like one sent from the page (`responses/<slug>`, then status `answered`). It shows on the page within a second, and the hooks deliver it the same way.
- Only your verified number is heard. Texts from anyone else are dropped without a reply.

Setup:
1. Make a Photon project just for the desk at https://app.photon.codes (or `npx @photon-ai/cli login`, then `npx @photon-ai/cli projects create --name "Decision desk" --platforms imessage`). On the Free and Pro plans, texts come from Photon's shared pool of numbers. On Business you can add a dedicated line.
2. Spectrum's SDK is TypeScript only, so a small Node script, `local/spectrum-bridge.mjs`, does the sending and receiving. Install Node 18.17 or later, then run `npm install --omit=dev` in `local/`. The server starts the script when it's needed and talks to it over a pipe; it opens no ports.
3. Run `python3 local/setup_texting.py` and paste the project id and secret from the project's Settings. The secret is typed hidden. The script checks them with read-only calls, writes `~/.local/state/decision-desk/spectrum.env` that only you can read, and waits until the running server has picked it up. No restart needed.
4. In the page, tap Text me and connect your phone. On a shared-pool project, Connect also registers your number as a user of the project, because those projects only text registered numbers.

Things to know:
- Replies come in over the SDK's own connection, so there is no webhook and nothing open to the internet.
- Use a project nothing else answers. Every webhook registered on a Spectrum project gets the texts sent to that project's lines, so if another app shares the project, it gets your replies too. The setup script warns you if the project has webhooks.
- Photon's deliverability guide prefers that people text the line first. The desk texts first (the code), so Apple may show a "Report Junk" banner until you save the number as a contact.

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

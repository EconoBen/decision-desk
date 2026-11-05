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
- `status`: open, answered, waiting, followup or done
- `agentNote`, `agentAt`, `changedAt`, `ticketNote` {label, url}
- `order`, `createdAt`

`responses/<same slug>` is written by the page: {choice, note, submittedAt}.

`digest/<area>` is an optional "What Claude did" log: {order, area, headline, period, items: [{text, url}], updatedAt}.

## Writing rules that made it work for Ben

- Write each decision for someone coming in cold, with no ticket shorthand. Use bullets, not paragraphs.
- Every choice gets a one-line Why or Why not, and every decision gets a Risks section, even if it's "None known".
- Never mark something done while a step remains. Use `followup` for "Your move" and `waiting` for "Waiting on others".
- Re-read Slack posts and threads, GitHub and Linear before writing a decision, and list them in `checked`.

# Design: team chat for the autopilot (Slack or Discord)

Status: **proposed**, 2026-11-27. Experimental, like the rest of the autopilot.

## 1. Goal

Make the autopilot team's conversation visible and steerable from one chat app,
so the product owner follows and directs the team the way they would follow a
human team, from a phone if they like.

- Every epic has a **thread**: its developer's questions, the lead's answers,
  gate and review results, decisions, merges, and the owner's notes.
- The run has a thread too: the plan to approve, progress, the final report to
  approve.
- The owner replies in those threads to answer questions, approve or reject a
  gate, stop the run, or leave guidance for an epic's developer.
- Threads are the team's **memory**: a developer that restarts, crashes, or
  is resumed weeks later reads its thread and picks up from there.
- **Exactly one chat app at a time, Slack or Discord**, swappable with one config
  change. History is never lost when switching.

### Non-goals (for now)

- Two chat apps at once, Telegram, voice or meetings.
- Agents chatting freely in real time. Developers and the lead are still workflow
  steps that run, finish and exit. The thread is their memory, not a live line.
- Long-running agent sessions per team member, as in the "AI company" reference
  architecture. The deterministic scheduler stays in charge.
- Websocket listeners (Slack Socket Mode, Discord Gateway). Polling every ~20 s
  is fast enough for a team that works in hours, and it needs only the Python
  standard library.

## 2. Vocabulary

| Term | Meaning |
| --- | --- |
| **Journal** | The team's conversation, stored in the repo as one file per thread. The source of truth. |
| **Thread** | One conversation. One per epic, plus one per run. |
| **Message** | One entry in a thread: who said it, what kind it is, the text, and structured tags. |
| **Chat app** | Slack or Discord: where humans see and answer the journal. |
| **Connector** | A small piece of code that knows one chat app's API. Exactly one is active. |
| **Bridge** | One background process (plain code, no AI) that copies the journal to the chat app and brings the owner's replies back. |
| **Owner** | The human product owner. Only their account's messages count. |

## 3. Architecture

```text
 developer / lead / reviewer steps        conductor (driver)          owner commands
 (consult, record, gate, decide)          (start, merge, halt...)     via agent chat (CLI)
            │                                    │                          │
            └──────────── append ────────────────┴──────────────────────────┘
                                   ▼
          Journal: .specify/autopilot/threads/<thread>.jsonl   (in git, platform-neutral)
                 │                                   ▲
       render    │                                   │ owner replies (journaled)
                 ▼                                   │
   context/thread.md  ◄── agents read          Bridge (one process, no AI)
   before every step                            │  outbound: deliver new messages
                                                │  inbound:  poll replies, run owner commands
                                                ▼
                                    Connector: slack  |  discord   (exactly one, or none)
```

Rules that keep this robust:

1. **Writers never touch the network.** A step appends one line to a journal
   file and moves on. A chat outage, a bad token or a rate limit can never fail
   a gate, a consult or a merge.
2. **The bridge is the only network user.** It holds the only token, so agents
   never see one.
3. **Agents never read the chat app.** They read `thread.md`, rendered from the
   journal. The journal only holds the team's own messages and the verified
   owner's replies, so text typed by anyone else never reaches an agent.
4. **The journal is complete without a chat app.** With `provider: none`,
   everything works as today, and the threads still exist as files.

## 4. The journal

### Files

```text
.specify/autopilot/threads/
├── run-<run-id>.jsonl      plan, approvals, progress, final report
└── <run-id>-<EPIC>.jsonl   one epic's whole story
```

- **Keys carry the run ID.** A later run's epic `E1` never mixes with an old
  one. The run ID is created by `plan-check`, for example `r1127-4f2a`.
- **The journal is committed.** The state directory's `.gitignore` lets
  `threads/*.jsonl` through. `land` commits it together with the merge, and when
  a run ends without landing, the `run` command offers to commit it.
- **Runtime-only, never part of an epic's diff.** Threads live in the
  conductor's checkout only. Like all of `.specify/autopilot/`, they are
  excluded from epic commits, review diffs and read-only fingerprints (see §12).

### Message schema (one JSON object per line)

```json
{
  "id": "18c2f0a1b2c3d4e5-9a1b2c",
  "at": "2026-11-27T09:14:02+00:00",
  "thread": "r1127-4f2a-E2",
  "author": "dev-E2",
  "kind": "question",
  "text": "Should a session expire after inactivity, or only on logout?",
  "meta": {"run": "r1127-4f2a", "epic": "E2", "question": "003-Q1"},
  "mention_owner": false,
  "origin": {"provider": "slack", "ref": "1732698842.001200"}
}
```

| Field | Notes |
| --- | --- |
| `author` | A role: `lead`, `dev-<EPIC>`, `reviewer`, `autopilot` (gate, scheduler, bridge), or `owner`. |
| `kind` | `plan`, `approval`, `event`, `question`, `answer`, `owner-question`, `decision`, `gate`, `review`, `report`, `note`, `reply`. |
| `meta` | Flat string values used for lookup: `run`, `epic`, `question`, `decision`, `event`, `iteration`, `status`. Shown as tags, never as prose. |
| `origin` | Set only on messages that came *from* the chat app (owner replies). The bridge never re-posts them. |

Concurrency: each epic's steps write only that epic's file, and the scheduler
and bridge write the run file. Each message is a single short `O_APPEND` write,
and readers skip a torn last line. Only the bridge writes delivery state.

### What gets posted (and what does not)

- **Posted:** summaries. "Iteration 2: tests ✗, lint ✓ → 3 fix tasks", the
  reviewer's blocking findings in one line each, questions, answers and
  decisions in full.
- **Never posted:** raw command output, diffs, environment details, or anything
  from `.env` or credential files. Gate output stays in the repo
  (`gate-N.md`); the message links to its path. This keeps secrets that a
  failing test prints out of the chat app.

## 5. Layout in the chat app

One place per project: **one Slack channel**, or **one Discord forum channel**.

| | Slack | Discord |
| --- | --- | --- |
| Run thread | A top-level message "🛰 Run r1127-4f2a: <product>" and its replies | A forum post titled "Run r1127-4f2a" |
| Epic thread | A top-level message "E2 · Sessions and login (r1127-4f2a)" and its replies | A forum post titled "E2 · Sessions and login" |
| Live status | The bot edits the top message's first line (`🟡 running · iteration 2/5`) | Forum **tags** on the post (`running`, `needs-owner`, `failed`, `merged`...) plus an edited first line |
| Who is speaking | Each message shows `lead`, `dev-E2`, `reviewer`, `autopilot` as its name | Same, via a webhook on the forum |
| Hidden metadata | Slack message metadata (`event_type: speckit_autopilot`, payload = `meta`) | None; the journal holds it |
| Finding things | Search, plus metadata for tools | Forum tags filter, search |

The owner is mentioned (`@you`) only when something needs them: a product
question, a gate waiting for approval, an epic that failed or escalated, or a
halted run. Everything else is posted quietly.

## 6. Connector interface

One Python module per chat app, standard library only (`urllib`). Each is
about 250 lines, and HTTP is injected so tests never touch the network.

```python
class Connector(Protocol):
    name: str                       # "slack" | "discord"
    caps: Caps                      # hidden_metadata, tags, edit, custom_names, max_text

    def check(self) -> CheckReport: ...
        # identity, permissions, channel/forum reachable, owner ID valid, tags present

    def open_thread(self, title: str, first: OutMessage, tags: list[str]) -> ThreadRef: ...
    def post(self, thread: ThreadRef, message: OutMessage) -> list[MessageRef]: ...
        # may split long text (Discord: 2000 chars per message)
    def set_status(self, thread: ThreadRef, status_line: str, tags: list[str]) -> None: ...
        # no-op where unsupported
    def fetch(self, thread: ThreadRef, after: Cursor | None) -> tuple[list[Inbound], Cursor]: ...
    def fetch_top_level(self, after: Cursor | None) -> tuple[list[Inbound], Cursor]: ...
    def acknowledge(self, ref: MessageRef, ok: bool) -> None: ...   # ✅ / ⚠️ reaction
```

- `OutMessage` is `{author, text, mention_owner, meta}`. The text is plain
  Markdown with `**bold**`, `` `code` ``, lists and links. Each connector
  converts it (Slack mrkdwn, Discord Markdown), escapes it, and adds the owner
  mention its own way.
- `Inbound` is `{ref, author_id, is_bot, text, at}`.
- **Errors:** every connector raises `ConnectorError(retryable, retry_after)`.
  The bridge handles back-off the same way for both apps: HTTP 429 honours
  `Retry-After`, 5xx and timeouts back off exponentially up to 5 minutes, and
  401/403 stop delivery and report `unhealthy` (never retried in a loop).

### Slack specifics

- **Setup:** a Slack app created from a manifest that ships with the extension
  (`slack-app-manifest.yml`). Scopes: `chat:write`, `chat:write.customize`,
  `channels:history` (or `groups:history` for a private channel),
  `reactions:write`. Install it to the workspace, invite the bot to the channel.
- **Rate limits:** an app you build for your own workspace keeps Slack's
  normal ("Tier 3") read limits. The 2025 cut to 1 read per minute applies only
  to apps sold to other workspaces outside the Slack Marketplace. The bridge
  still polls only active threads.
- **Free plan:** works. Threads older than 90 days disappear from Slack, but the
  journal keeps them, and agents read the journal anyway.

### Discord specifics

- **Setup:** a bot in the Discord developer portal with the **Message Content**
  intent switched on (needed to read the owner's replies). It is invited to the
  server with View Channel, Send Messages, Send Messages in Threads, Read
  Message History, Add Reactions, Manage Threads (to set tags) and Manage
  Webhooks (to create the speaker webhook once).
- **Posting:** through one webhook on the forum. The webhook sets the speaker's
  name per message and posts into a post's thread with `thread_id`. Reading
  uses the bot token: `GET /channels/{thread}/messages?after=`.
- **Tags:** `check` creates the missing status tags on the forum (Discord allows
  20 per forum, 5 per post). The connector applies at most: one kind tag
  (`run`/`epic`) plus one status tag.
- **Archived posts:** a post that goes quiet is archived after the forum's
  inactivity time. The connector unarchives a post before writing to it.

## 7. The bridge

`autopilot.py chat bridge [--detach]`: one process per project, guarded by the
same atomic process lock as `drive` (§12).

One tick, every `chat.poll_seconds` (default 20):

1. **Mirror events.** Read new lines of `log.jsonl` (the scheduler's event log)
   from a saved offset, and turn them into `event` messages in the right
   thread: started, passed, merged, failed, escalated, blocked, halted,
   finished.
2. **Deliver.** For each thread, post journal lines after the last delivered
   one, in order. The first post creates the thread. Status changes update the
   thread's status line and tags. A failure stops that thread for this tick,
   keeping order.
3. **Listen.** Poll the run thread and **active** epic threads (not merged, or
   with activity in the last 24 h), plus top-level messages. Ignore everything
   not from the configured owner ID. Journal each owner message (with `origin`),
   then run it as a command or keep it as a note (§8). Acknowledge with a
   reaction.
4. **Health.** Write `chat/health.json`:
   `{provider, ok, last_ok_at, last_error, pending, bridge_pid}`.

Delivery state is local, per provider, and not committed:
`.specify/autopilot/chat/<provider>.json`. It holds the remote thread for each
journal thread, the count of delivered lines, and the inbound cursors.

**Lifecycle:**

- `run` starts the bridge as soon as a plan exists (gate 1 needs it).
- `drive --detach` restarts it if it died.
- The bridge exits by itself once the run is landed or rejected and everything
  is delivered. `chat stop` stops it.

**Flood guard:** at most 20 posts per minute in total. Beyond that, events for
one thread are coalesced into one summary message. This keeps a runaway loop
from spamming the chat app or hitting its limits.

## 8. What the owner can say

Messages are read only from the configured owner ID. Commands are matched
case-insensitively after stripping a leading bot mention.

| Message | Where | Effect |
| --- | --- | --- |
| `approve plan <run-id>` / `reject plan <run-id> <why>` | run thread | Gate 1. Approve records it and starts `drive`. Reject records it and ends the run; the reason goes back to the lead for re-planning. |
| `approve land <run-id>` / `reject land <run-id> <why>` | run thread | Gate 2. Approve records it and runs `land`. Reject records it; the integration branch stays for inspection. |
| `answer <question-id> <text>` | anywhere | Answers that product question. |
| Any other text, when the epic has **exactly one** open product question | that epic's thread | Answers that question. The bot replies "Recorded as the answer to 003-Q1". |
| `stop` / `resume` | run thread | Same as the `stop` / `unstop` CLI: no new epics start / starting resumes. |
| `status` | anywhere | The bot replies with the stage, the epic table and the open questions. |
| Anything else | an epic thread | A **note**: guidance for that epic. The developer sees it in `thread.md` and must follow it like a decision. |
| Anything else | the run thread | A note for the whole team: every developer's `thread.md` shows run-thread notes at the top. |

**Approvals are written and exact.** The run ID in the phrase stops an old
"approve" from approving a new run, and a reaction or "ok" does not count.
Approvals from chat and from the agent chat (`autopilot.py approve ...`) go
through the same function and are recorded in
`.specify/autopilot/approvals.json` with who, where and when. `drive` refuses
to start without a recorded plan approval for the current run ID. `land`
refuses without a land approval. A rejection blocks both, which also closes a
review finding (§12).

## 9. What agents see

Before every developer step (specify, plan, tasks, implement, converge) and
every lead consult, the step's context gets `context/thread.md`:

```markdown
# Team thread: E2 · Sessions and login (run r1127-4f2a)

## Notes from the product owner (whole team)
- 2026-11-27 09:02 · "Use the existing Postgres; no new infrastructure."

## This epic, oldest first
**Developer (E2)** · 09:14 · question=003-Q1
Should a session expire after inactivity, or only on logout?

**Team lead** · 09:16 · decision=D-003-Q1
After 30 minutes of inactivity; the handbook's security section requires it.

**Autopilot** · 10:40 · iteration=2
Gate: tests ✗ (2 failing), lint ✓ → 2 fix tasks added. Report: .specify/autopilot/pipes/003-sessions/gate-2.md
...
```

It shows the last 80 messages, each cut to 1,500 characters. The workflow's
team-context instruction lists `thread.md` next to `program.md`,
`epic-context.md` and `decisions.md`.

## 10. Plug, unplug, switch

| Action | How | What happens |
| --- | --- | --- |
| **Plug in** | `/speckit.autopilot.chat connect slack` (or `discord`) | Walks the owner through creating the app or bot, writes the non-secret settings, runs `check`, and posts a hello message. |
| **Unplug** | `/speckit.autopilot.chat disconnect` | Sets `provider: none` and stops the bridge. The journal keeps growing, and nothing else changes. |
| **Switch** | `/speckit.autopilot.chat connect discord` while on Slack | Same as plug in, then **replay**: the new connector recreates the threads of the current and previous runs from the journal. Replayed messages are marked "(from <date>, by <role>)". The old chat app is left untouched. |

The bridge refuses to post into a provider whose delivery state is empty while
the journal is not, unless replay was confirmed. This avoids an accidental
flood after a clone or a deleted state file.

## 11. Configuration

Non-secret settings follow the extension's layered config: `autopilot-config.yml`,
then `local-config.yml`, then `SPECKIT_AUTOPILOT_*` environment variables.

```yaml
chat:
  provider: none            # none | slack | discord
  poll_seconds: 20
  slack:
    channel_id: ""          # C0123ABCD (a public channel keeps Slack export possible)
    owner_id: ""            # your Slack member ID, U0123ABCD
    token_env: SLACK_BOT_TOKEN
  discord:
    forum_channel_id: ""    # the project's forum channel
    owner_id: ""            # your Discord user ID
    token_env: DISCORD_BOT_TOKEN
```

**Secrets are never in config.** The bot token comes from the environment
variable named by `token_env`, or from `~/.config/specify/autopilot/<provider>-token`
(mode 0600). The Discord webhook is created by `check` and stored in that same
token file, never in the repo.

Honest limit: an agent with shell access can read its own environment. The
bridge therefore reads the token file directly, and the docs recommend the file
over an exported variable.

## 12. How this shapes the review fixes

Findings from the review of PR #5588 that this design touches. They are fixed
first, in the shape described here:

| Finding | Shape of the fix |
| --- | --- |
| Drive lock is not atomic | One `ProcessLock` helper (exclusive create plus a recorded PID, with stale-owner takeover that is safe against races), used by `drive` **and** `chat bridge`. |
| Timeouts kill only the parent process | Process-group launch and tree kill in `util.run`. The bridge's spawned `drive` uses the same helper. |
| Layered config and env overrides ignored | One loader for all sections, including `chat`. Secrets are excluded by design. |
| Rejecting a gate doesn't stop `drive`/`land` | `approvals.json` is enforced by `drive` and `land` (§8). |
| Stale `halted` state; no way to continue a standalone answer | One `answer` path shared by the CLI, agent chat and the bridge. It reopens the pipe and clears stale halts. |
| Unsafe decision IDs | One `safe_id` validator for question, decision, epic and run IDs. These now also appear in chat commands. |
| Unreadable answer files crash `status` | Corrupt files count as unanswered. The bridge reads questions constantly, so this must hold. |
| Fingerprints catch runtime files; HEAD missing | Fingerprints include `HEAD` and exclude all of `.specify/autopilot/` and `.specify/workflows/runs/`. Journal writes in standalone mode must not look like a reviewer's edit. |
| Answer decided but context stale for the next step | `record` refreshes `decisions.md` **and** `thread.md` after publishing. |
| `converge` step lacks team context | Its instruction gets the full team context, including `thread.md`. |

The remaining findings (staging, pathspecs, config validation, reset and
resource reuse, budget accounting, spec scenario checks, worktree setup
markers) don't depend on this design and are fixed as they stand.

## 13. Testing

- **Core and bridge:** an in-memory fake connector. Tests cover:
  - ordering and resuming delivery after a failure, and the flood guard;
  - owner-only filtering, the command grammar, the single-open-question rule;
  - exact approvals with the run ID, and that approvals are enforced by
    `drive` and `land`;
  - that a replay guard is required, and that `thread.md` renders correctly.
- **Connectors:** request shapes against a fake HTTP function (URLs, headers,
  bodies, splitting, escaping), 429 and `Retry-After`, 401 handling, and
  cursor paging. No live network in CI.
- **Manual smoke test:** a documented checklist against a real Slack workspace
  and a real Discord server, before marking a connector "works".

## 14. Delivery plan

1. **Review fixes**, shaped by §12, each with regression tests.
2. **Core:** journal, `thread.md`, approvals, the bridge with the fake
   connector, and the CLI (`chat check|bridge|stop|replay|status`, `approve`).
3. **Slack connector**, the app manifest, `/speckit.autopilot.chat`, and docs.
4. **Discord connector**, replay and switching.
5. Changelog entry, then the experimental tag `v1.4.5-autopilot.2`.

## 15. Decisions taken in discussion

- One chat app at a time; Slack or Discord.
- One Discord forum per project; the same idea is one Slack channel per project.
- The owner approves gates in chat with a written, exact phrase. Reactions don't
  count.
- Threads are the team's memory. Humans read them in the chat app, agents read
  them from the journal.
- Slack's free plan is acceptable to start; the journal makes its 90-day limit
  harmless.

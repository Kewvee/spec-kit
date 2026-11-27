---
description: "Connect the autopilot team to Slack or Discord (one at a time), switch between them, disconnect, or check the connection"
---

# Autopilot Team Chat

Connect the autopilot team to one chat app so the product owner can follow every epic's thread and reply there: approve the plan and the final merge, answer product questions, stop the run, or leave notes the developers will follow. The team's conversation always lives in the repository journal (`.specify/autopilot/threads/`); the chat app is a window onto it. Exactly one app is connected at a time.

## User Input

```text
$ARGUMENTS
```

One of: `connect slack`, `connect discord`, `disconnect`, `check` (the default when empty).

`AP` below means `python3 .specify/extensions/autopilot/scripts/python/autopilot.py` from the repository root (`python` where `python3` is unavailable). Every `AP` call prints one JSON object.

## Rules

- **Never ask for, accept or repeat a token in this conversation.** The user puts the bot token into a private file or an environment variable themselves. If they paste one here anyway, tell them to revoke it in the app's settings and create a new one.
- Write chat settings to `.specify/extensions/autopilot/local-config.yml` (machine-local, not committed). Use the same keys as the `chat:` section of `autopilot-config.yml`, and change nothing else in that file. IDs are not secrets, but they are personal.
- Only the configured `owner_id`'s messages count. Make sure it is the user's own ID, not a teammate's.

## check

Run `AP chat status`, then `AP chat check`. Report:

- the provider and whether the bridge is running;
- `ok` or each problem, in plain words with its fix;
- `pending` messages and the last error from the health file.

If `provider` is `none`, offer `connect slack` or `connect discord`.

## connect slack

1. Guide the user through creating the app:
   - At <https://api.slack.com/apps>, choose **Create New App**, then **From an app manifest**, and paste `.specify/extensions/autopilot/chat/slack-app-manifest.yml`.
   - Click **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-…`).
   - Store the token themselves, in a terminal of their own, without pasting it here. Suggest: `mkdir -p ~/.config/specify/autopilot && (umask 077; read -rs -p 'token: ' t && printf '%s\n' "$t" > ~/.config/specify/autopilot/slack-token)`.
   - Create or pick one channel for this project. A public channel keeps Slack's export possible. Invite the bot with `/invite @spec-kit autopilot`.
2. Ask for two IDs:
   - the **channel ID**: channel name → About → at the bottom, like `C0123ABCD`;
   - the user's **member ID**: their profile → ⋮ → Copy member ID, like `U0123ABCD`.
3. Write the settings to `local-config.yml`:
   ```yaml
   chat:
     provider: slack
     slack:
       channel_id: C0123ABCD
       owner_id: U0123ABCD
   ```
4. Run `AP chat check --hello`. On problems, explain the fix and run it again. On success, the channel shows an "Autopilot connected" thread mentioning the user.
5. Finish with **Switching and history** below.

## connect discord

1. Guide the user through creating the bot:
   - At <https://discord.com/developers/applications>, create a **New Application**. Under **Bot**, use **Reset Token** and copy the token. On the same page, switch on **Message Content Intent**.
   - Store the token themselves, in a terminal of their own: `mkdir -p ~/.config/specify/autopilot && (umask 077; read -rs -p 'token: ' t && printf '%s\n' "$t" > ~/.config/specify/autopilot/discord-token)`.
   - In **OAuth2 → URL Generator**, choose scope `bot` and these permissions:
     - View Channels, Send Messages, Send Messages in Threads, Read Message History, Add Reactions;
     - Manage Threads, for the status tags;
     - Manage Webhooks, for the speaker names;
     - Manage Channels, needed only once to create the forum tags.
     
     Open the URL and add the bot to their server.
   - Create a **Forum** channel for this project.
2. Ask for two IDs. Turn on Developer Mode (User Settings → Advanced) first, then right-click to copy:
   - the **forum channel ID**;
   - the user's **own user ID**.
3. Write the settings to `local-config.yml`:
   ```yaml
   chat:
     provider: discord
     discord:
       forum_channel_id: "123456789012345678"
       owner_id: "234567890123456789"
   ```
   Quote the IDs.
4. Run `AP chat check --hello`. It also creates the status tags and the speaker webhook (stored in `~/.config/specify/autopilot/`, never in the repo). Fix and repeat until it passes.
5. Finish with **Switching and history** below.

## Switching and history

After connecting, run `AP chat status`. If the health shows that replay confirmation is needed, the journal already holds conversations this app has never seen: an earlier run, or the app used before a switch.

- Ask the user whether to post that history into the new app. Replayed messages are marked with their original time.
- On yes, run `AP chat replay --confirm`. On no, tell them the history stays in the repository journal, and the agents still read it.

Then run `AP chat bridge --detach`. The bridge also starts by itself whenever a plan is checked or the drive starts. When switching apps, the old app keeps its threads untouched; nothing is deleted.

## disconnect

Run `AP chat stop`, then set `chat.provider: none` in `local-config.yml`. The team keeps working and the journal keeps growing. Reconnecting later replays what was missed, after confirmation.

## What the owner can say in chat

Tell the user once, after a successful connect:

| Message | Where | Effect |
| --- | --- | --- |
| `approve plan <run-id>` / `reject plan <run-id> <why>` | run thread | Gate 1: starts the team, or sends the plan back |
| `approve land <run-id>` / `reject land <run-id> <why>` | run thread | Gate 2: merges into the base branch, or holds |
| `answer <question-id> <decision>` | anywhere | Answers that product question |
| any reply when the epic has exactly one open question | epic thread | Answers it |
| `stop`, `resume`, `status` | run thread | Stop new epics, undo the stop, show where things stand |
| anything else | a thread | A note the developers must follow (in the run thread: for the whole team) |

Approvals count only with the exact phrase and the current run ID, from the owner's account. A reaction or an "ok" does not count.

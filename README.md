# Michi

A local, single-user agentic assistant that builds a persistent model of one
person's projects, deadlines, habits, and interests, and uses it to stay ahead
of them rather than wait to be asked.

It runs as a CLI, as a voice session, and as a small web UI where kept
commitments grow a sakura tree.

Implements **Phase 0–5** of the project spec. Telegram was dropped by decision
(see *Deliberately out of scope*); Phase 6 was never in v1.

> **Location matters.** This lives at `~/digital-twin`, deliberately *not*
> under `~/Desktop` or `~/Documents`. Those are TCC-protected on macOS, and a
> launchd background agent cannot read them — the scheduler fails before Python
> even starts. If you move this project, keep it out of those folders.

## Setup

```bash
cd ~/digital-twin
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in your key — see the Agent core section
createdb digital_twin
PYTHONPATH=. .venv/bin/python scripts/init_db.py
```

Then run onboarding:

```bash
PYTHONPATH=. .venv/bin/python -m twin.cli onboard
```

## Commands

```bash
PYTHONPATH=. .venv/bin/python -m twin.cli chat      # talk to the twin
PYTHONPATH=. .venv/bin/python -m twin.cli voice     # talk to it out loud
PYTHONPATH=. .venv/bin/python -m twin.cli status    # what it currently knows
PYTHONPATH=. .venv/bin/python -m twin.cli check     # run proactive checks once
PYTHONPATH=. .venv/bin/python -m twin.cli watch     # run them on an interval
PYTHONPATH=. .venv/bin/python -m twin.cli service   # ...or in the background
PYTHONPATH=. .venv/bin/python -m twin.cli slack-listen  # live Slack events
PYTHONPATH=. .venv/bin/python -m twin.cli gmail-watch   # Gmail push status
PYTHONPATH=. .venv/bin/python -m twin.cli onboard   # re-run as settings
```

`chat` and `voice` resume from prior conversation history by default; `--fresh`
starts clean. If nudges are queued, the twin opens the conversation itself.

## Running in the background

The persona only works if the proactive checks actually run, and `watch` stops
the moment its terminal closes. `service` installs a **LaunchAgent** that runs
`check` on an interval regardless:

```bash
PYTHONPATH=. .venv/bin/python -m twin.cli service --install --interval 1800
PYTHONPATH=. .venv/bin/python -m twin.cli service --logs      # recent output
PYTHONPATH=. .venv/bin/python -m twin.cli service --run-now   # trigger a run
PYTHONPATH=. .venv/bin/python -m twin.cli service --uninstall
```

It runs `check` on a `StartInterval` rather than keeping `watch` alive under
`KeepAlive`, because launchd already does scheduling, supervision,
restart-on-crash, and catch-up after sleep. A long-lived APScheduler process
would duplicate all of that and add something that can die unnoticed.

An *agent* rather than a daemon, because the credential store is the login
keychain, which only exists inside a GUI session.

**The first background run may prompt for keychain access.** Choose *Always
Allow* — otherwise background runs silently fail to reach your accounts while
appearing to succeed. Logs live in `~/Library/Logs/digital-twin/`; check
`scheduler.err.log` first if something looks wrong.

## Connecting Slack

Slack's OAuth flow requires an HTTPS redirect URL, which a local-only app has
nowhere to host — so you create your own Slack app and paste the tokens during
onboarding. At [api.slack.com/apps](https://api.slack.com/apps), create an app
from scratch, then:

1. **OAuth & Permissions** → add the bot scopes `channels:read`,
   `channels:history`, `groups:read`, `groups:history`, `im:read`, `im:history`,
   `users:read`, `chat:write`. Install to the workspace and copy the **Bot User
   OAuth Token** (`xoxb-`). This is the only required token.
2. *Optional* — **Basic Information → App-Level Tokens**: create one with
   `connections:write` (`xapp-`) to enable `slack-listen`.
3. *Optional* — a **User OAuth Token** (`xoxp-`) with `search:read` enables
   `slack_search`. Bot tokens cannot call `search.messages`; this is a Slack
   restriction, not a gap here.

The bot only sees channels it's a member of — `/invite @your-app-name` in any
channel you want it reading. Tokens go to the OS keychain, not disk.

## Connecting Notion

Create an integration at [notion.so/my-integrations](https://www.notion.so/my-integrations)
(New integration → Internal), copy its **Internal Integration Secret**, and
paste it during onboarding.

**Then share your pages with it.** A Notion integration sees *nothing* by
default — in Notion, open the page or database, click **...** → **Connections**
→ add your integration. Sharing a parent page shares everything beneath it.
This is the failure everyone hits first, so the adapter translates Notion's
bare `object_not_found` into those instructions.

The useful tool here is `notion_sync_projects`: point it at a Notion database
of projects and each row becomes a tracked project, so the staleness nudges
run against real project state instead of only what you've mentioned in chat.

## Connecting Todoist

In Todoist, go to **Settings → Integrations → Developer** and copy your **API
token**, then paste it during onboarding.

Once connected, dated tasks are imported as tracked deadlines on the scheduled
sweep, alongside those found in email and calendar.

## Voice

`voice` needs `OPENAI_API_KEY` (one key covers Whisper and TTS) and a
microphone. Press Enter to start recording, Enter again to stop; you can also
just type mid-session, and say or type "quit" to leave.

On macOS the first run triggers a microphone permission prompt for your
terminal — if it doesn't appear, enable it under System Settings → Privacy &
Security → Microphone. PortAudio ships inside the `sounddevice` wheel on
macOS and Windows; on Linux also `apt install libportaudio2`.

## Connecting Gmail and Calendar

Both need an OAuth client you own:

1. Google Cloud Console → create a project → enable the **Gmail API** and
   **Google Calendar API**.
2. Credentials → Create OAuth client ID → application type **Desktop app**.
3. Download the JSON, save it in this directory, and point
   `GOOGLE_CLIENT_SECRETS_FILE` at it in `.env`.
4. Re-run `onboard` and select them. Tokens go to the OS keychain, not disk.

### Gmail push notifications (optional)

By default Gmail is polled on a cooldown. To switch to push, in the *same*
Google Cloud project as your OAuth client:

1. Enable the **Cloud Pub/Sub API**.
2. Create a topic, e.g. `gmail-push`.
3. **Grant `gmail-api-push@system.gserviceaccount.com` the Pub/Sub Publisher
   role on that topic.** This is the step everyone misses — without it Gmail
   silently refuses to register the watch.
4. Create a **pull** subscription on that topic (not push — the whole point is
   that nothing listens on a port).
5. Put both full resource names in `.env`:
   ```
   GMAIL_PUBSUB_TOPIC=projects/<proj>/topics/gmail-push
   GMAIL_PUBSUB_SUBSCRIPTION=projects/<proj>/subscriptions/gmail-pull
   ```
6. Re-run `onboard` — enabling push adds the `pubsub` OAuth scope, which
   invalidates the stored token.
7. Register the watch:
   ```bash
   PYTHONPATH=. .venv/bin/python -m twin.cli gmail-watch --start
   ```

After that, `watch` renews the subscription automatically and drains
notifications each tick. `gmail-watch` on its own shows current state;
`gmail-watch --pull` drains once immediately.

## Layout

```
twin/
  agent.py            single tool-calling loop + persona + context assembly
  onboarding.py       first-run picker, questionnaire, settings
  scheduler.py        proactive checks (staleness, deadlines, free time)
  cli.py              entrypoint, confirmation UI
  config.py           env settings, read/write OAuth scope split
  credentials.py      keychain-backed token store
  timeutil.py         ISO parsing, timezone resolution, active windows
  service.py          launchd agent definition + install/uninstall
  aux.py              cheap model calls via OpenRouter + JSON extraction
  extraction.py       shared deadline classify/extract for mail, calendar, Slack
  memory/
    schema.sql        profile, projects, deadlines, history, embeddings, nudges
    store.py          all reads/writes of the twin's model
    embeddings.py     optional semantic recall
  tools/
    registry.py       MCP-shaped tool registry with a write flag
    memory_tools.py   10 always-available tools
    gmail_tools.py    search / read / triage / draft-reply / deadline sweep
    gmail_watch.py    Pub/Sub pull trigger layer + watch renewal
    calendar_tools.py list / free-time / conflicts / create / reschedule
    opportunity_tools.py  Exa search, relevance scoring, surfaced-URL dedup
    slack_tools.py    channels / history / threads / search / post
    slack_auth.py     token storage + Slack error translation
    slack_listener.py Socket Mode trigger layer
    notion_tools.py   search / read / query / sync-projects / create
    notion_auth.py    token storage, pinned API version, error translation
    todoist_tools.py  projects / tasks / sync-deadlines / create / complete
    todoist_auth.py   token storage + cursor pagination
    google_auth.py    OAuth flow
  voice/
    audio.py          mic capture, WAV assembly, silence gate, playback
    stt.py            Whisper transcription
    tts.py            speech synthesis + text-for-speech cleanup
    session.py        the voice REPL
```

37 tools total: 11 always available, 5 requiring Gmail, 6 requiring Calendar,
5 requiring Slack, 5 requiring Notion, 5 requiring Todoist. Seven are writes
(`gmail_draft_reply`, `calendar_create_event`, `calendar_reschedule_event`,
`slack_post_message`, `notion_create_page`, `todoist_create_task`,
`todoist_complete_task`) and all seven are gated on confirmation.

## Design notes

**One agent, not an orchestration.** Per spec section 5, proactive checks are
a scheduled job that queues nudges; the agent core does all the talking, so
nudges arrive in the persona's voice rather than as canned strings.

**Write actions are gated in the loop, not by the prompt.** Every tool carries
a `write` flag. `Agent._run_tool` refuses to invoke a write handler without an
explicit confirmation callback returning `True`, and the default callback
denies. A declined action is reported back to the model as a normal result
("the user declined, do not retry") rather than an error. Covered by
`tests/test_agent_loop.py`.

**Tools are gated on what's actually connected.** `REGISTRY.available()`
filters by the profile's `connected_apps`, so the model is never shown a Gmail
tool it can't authenticate.

**Free-time windows are computed, never stored.** Per spec section 7, they're
derived from calendar gaps on demand — any cached copy is wrong the moment an
event moves. The interval maths (`merge_intervals`, `subtract_busy`,
`compute_free_windows`) is pure and separated from the API calls, so it's
tested directly without network access.

**Availability is a user setting, not a default.** Timezone and active hours
are collected during onboarding rather than assumed, because a hardcoded 9-to-5
would be exactly the life-stage assumption spec section 3 rules out. Onboarding
*suggests* a timezone detected from the host, but the user confirms it.

**"Busy" is narrower than "has an event".** Events the user declined, and ones
marked transparent/free, appear in listings but don't consume time. All-day
events block the whole day, and Google's exclusive end-date is corrected for.

**Opportunity discovery is two-stage.** Exa does broad neural retrieval, then a
cheap auxiliary model scores each candidate against the user's actual
interests; the expensive agent model never sees the raw result dump. This is
spec section 10's split, and it's also the quality filter — raw web search
returns articles *about* hackathons far more often than hackathons.

If the scorer fails, discovery returns **nothing** rather than falling back to
unfiltered results. Surfacing an unvetted search dump to a user who trusts the
filter is worse than surfacing nothing, so that path is tested explicitly.

**Discovery is on a cooldown and dedupes what it has shown.** Unlike the other
proactive checks, this one spends API credits per run, so it scans at most
every 12 hours and records the scan even when it finds nothing. Surfaced
opportunities are written to the `embeddings` table under `kind='opportunity'`,
which gives repeat-suppression and semantic recall from one write.

**Speech never authorizes a write.** In a voice session the confirmation
prompt is announced aloud but the answer must still be *typed*. A
mis-transcribed "yes" is precisely the failure that must not be able to send an
email on the user's behalf, so `voice_confirm_write` speaks the notice and then
falls through to the same typed gate `chat` uses.

**The agent is untouched by voice.** Per spec section 9, STT and TTS sit
outside the tool-calling loop — the session converts audio to the same strings
`chat` already passes to `Agent.send`. Swapping either half for a local model
means reimplementing one function. The three I/O calls are injected via
`VoiceIO`, which is how the session loop is tested without hardware.

**Replies are rewritten before they're spoken.** Agent output is written for a
terminal and is full of markdown and URLs; `for_speech()` keeps link labels,
drops targets, strips formatting, and truncates at a sentence boundary. Reading
an opportunity nudge aloud verbatim would otherwise be mostly URL.

**Silence is filtered locally.** A capture that is too short or too quiet never
reaches the API, so a stray double-Enter costs nothing.

**Slack uses Socket Mode, not the Events API.** Spec section 6 names the Events
API, but that needs a public HTTPS endpoint, which conflicts with section 5's
local-machine-only rule. Socket Mode delivers the same events over an
*outbound* WebSocket with nothing listening on a port — the same reasoning the
spec already applies to Gmail's Pub/Sub *pull* subscription.

**LLM extraction is gated to messages that concern the user.** The listener
indexes everything it sees into recall (cheap, local), but only asks the
auxiliary model "was a commitment made here?" for messages the user wrote, was
mentioned in, or sent as a DM. Without that gate, a busy workspace would fire a
model call per message. Extraction below 0.6 confidence, or without a
resolvable date, is discarded — a wrong deadline is worse than a missed one.

**Slack redelivers events, so the listener dedupes by event id.** The id cache
is a bounded deque plus a set, and eviction removes from both — a set that grew
forever would be a slow leak in a long-running listener.

**Deadline extraction is two-stage, and the cheap stage runs first.** A
mailbox is mostly newsletters, so a classifier reads only sender, subject, and
snippet to pick candidates; full bodies are fetched *only* for those. Gmail's
own `-category:promotions -category:social -category:forums` filtering runs
before that, server-side, so the bulk of mail costs nothing at all. The
expensive agent model never sees raw mail — exactly the split spec section 10
describes.

**Calendar extraction skips the second stage entirely.** An event already
carries its own time, so the event's start *is* the due date. Classifying
"is this a deadline or an ordinary meeting?" is all that's needed, and it
avoids inventing a date the calendar already told us.

**The sweep is incremental and clamped.** `last_deadline_scan` doubles as the
Gmail `after:` cursor, so repeat runs only read new mail — but a cursor older
than 14 days is clamped, so a twin left off for months doesn't try to classify
months of mail in one run. The cursor only advances if a source actually ran;
otherwise a failed sweep would silently skip the mail it never read.

**All three sources share one validator.** Slack, email, and calendar feed the
same `parse_extraction`, which discards anything below 0.6 confidence or
without a resolvable date. Re-running is safe because `deadlines` is unique on
`(source, source_ref)` — a message id or event id — so an overlapping window
updates rather than duplicates.

**Notion sync preserves Notion's own timestamps.** `store.upsert_project`
normally stamps `last_activity_at` as now, which is right when the user just
mentioned a project. Sync passes Notion's `last_edited_time` instead — stamping
imported projects as active today would make every one look fresh and silence
the staleness nudge the import exists to feed.

**The Notion API version is pinned, deliberately.** Notion ships
backwards-incompatible versions; the 2025-09-03 release moved databases to a
*data source* model, deprecating `/v1/databases/:id/query` in favour of
`PATCH /v1/data_sources/:id/query` and changing the search filter from
`database` to `data_source`. The adapter calls Notion over httpx with
`Notion-Version: 2026-03-11` set explicitly, rather than through the SDK, so an
upgrade can't silently move that header underneath us. Tests assert the
endpoint shapes so a regression to the old API fails loudly.

Because most Notion URLs contain a *database* id rather than a data source id,
`notion_query_database` resolves one to the other automatically instead of
making it a user error.

**Gmail push uses a pull subscription.** Gmail publishes change notifications
to a Pub/Sub topic, and the usual way to consume that is an HTTPS push
endpoint — which a local-only app has nowhere to host. A *pull* subscription
inverts it: this process asks Pub/Sub for pending messages, so nothing listens
on a port. Same reasoning as Slack's Socket Mode, and exactly what spec
section 6 specifies.

**Watch renewal lives on the scheduler because the failure is silent.** A
Gmail watch stops delivering after 7 days with no error — mail would just
quietly stop arriving. It's renewed when within 24 hours of expiry, so a
missed tick can't let it lapse.

**Notifications carry no mail.** A Pub/Sub message is only
`{emailAddress, historyId}` — a nudge to go look. `history().list()` replays
what changed. Because Gmail coalesces notifications, a batch can span several
history ids, and the replay starts from the **lowest**; starting from the
highest would skip everything in between. Compared numerically, not
lexically — `"1000" < "900"` as strings.

**An expired history cursor is recovered, not ignored.** Gmail ages out old
history ids and answers 404. That's caught and turned into a short time-window
sweep plus a re-based cursor, so nothing is lost silently.

**Acknowledgement happens only after processing succeeds**, so a crash
mid-way leaves notifications un-acked and Pub/Sub redelivers them. Malformed
notifications are the exception — they're acked immediately, since they would
otherwise redeliver forever.

**The pubsub OAuth scope is only requested when push is configured**, so users
staying on the polling sweep are never asked to authorize a capability they
won't use.

**Todoist needs no model call.** A Todoist task already carries a structured
date, so the sweep imports it directly — inferring a date the task already
states would only add a way to be wrong. It's a third source in the same
scheduled sweep, but the cheapest one.

**Todoist's `due` and `deadline` are different things**, and either can exist
alone: `due` is when you plan to work on something, `deadline` is when it must
actually be done. `deadline` wins when present. A date with no time resolves to
**end of day in the user's timezone** — "due Friday" means by the end of
Friday, and treating it as midnight UTC would shift every all-day deadline.

**Recurring tasks are skipped.** A daily chore regenerating forever isn't a
deadline, and letting them in would bury the real ones. Overridable per call.

**List endpoints are cursor-paginated and the client follows through.** A
single-page read would silently import only part of someone's task list; the
loop is also bounded, so a server that always returns a cursor can't hang the
sweep.

**Todoist API v1, not REST v2.** Todoist deprecated both REST v2 and Sync v9
in favour of the unified `https://api.todoist.com/api/v1`. The old paths
redirect, but the redirect carries breaking changes — ids became opaque
strings, paths are strictly lowercase — so the new base is addressed directly.

**Slack's wire format is unwrapped before use.** `<@U123>`, `<#C456|eng>`, and
`<https://x|label>` become `@sarah`, `#eng`, and `label`. Passed through raw
they waste tokens and are unintelligible when read aloud.

**No pgvector.** The extension isn't available on this Postgres install, so
vectors are stored as `REAL[]` and cosine similarity runs in numpy. At
single-user volume that's a few milliseconds. Swapping in pgvector later means
changing one column type and one query in `store.search_similar()`.

**Embeddings are optional.** With no `OPENAI_API_KEY`, `recall` degrades to
keyword search instead of failing, so the assistant runs end to end on an
Anthropic key alone.

**macOS, not Windows.** `keyring` selects the Keychain backend here and
Credential Manager on Windows with no code change. Only Python 3.9 is
available on this machine, so the code stays 3.9-compatible (no `X | None`).

## Spec deviation: Eventbrite and Meetup are not usable

Spec section 8 names Eventbrite and Meetup as structured supplements to Exa.
Both have since closed public discovery:

- **Eventbrite** removed public access to the Event Search API in December 2019
  and began denying all requests in February 2020. The remaining endpoints
  retrieve events by id, venue, or organization — you can read events you
  already own, not discover arbitrary ones.
- **Meetup** consolidated onto a GraphQL API in February 2025, and creating the
  OAuth consumer needed to call it requires a paid Meetup Pro subscription.

So **Exa is the only source**, which the spec already prefers anyway. Rather
than ship adapters that silently return nothing, `opportunity_tools` documents
the seam (`STRUCTURED_SOURCES`) where a structured source would plug in,
returning the same normalized shape as `_normalize()`. Ticketmaster's Discovery
API is the closest working substitute if structured event data becomes
necessary.

## Deliberately out of scope

- **Telegram** (spec §8 Tier 2) — dropped by decision, not omission. A
  Telegram bot only sees chats it has been added to, never personal history,
  so it would add an integration surface without adding much the twin can use.
- **Tier 3** (WhatsApp, iMessage, SMS) — excluded by the spec itself (§8), and
  unchanged: no official API for personal history on any of them.
- **Eventbrite / Meetup** — see the section above; their public discovery APIs
  are closed.

## Not built yet

- Voice barge-in / continuous listening — v1 is push-to-talk only
- Voice I/O — Phase 4
- Slack / Notion / Todoist / Telegram — Phase 5
- Nothing prunes `conversation_history` or `embeddings`, and `search_similar`
  loads every vector per query — fine at hundreds of rows, slower at tens of
  thousands.
- launchd doesn't rotate logs, so `~/Library/Logs/digital-twin/scheduler.log`
  grows without bound. Slowly — one short run every 30 minutes — but it grows.

Two paths aren't exercised by the suite because they need live credentials:
the microphone capture → Whisper round-trip, and the Slack WebSocket itself.
Everything around both is tested against injected I/O — 39 voice tests and 49
Slack tests — and audio playback was verified against a real mp3 via `afplay`.

## Tests

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/ -q
```

## Resetting the twin

The database currently holds a couple of seeded rows used to verify the
proactive checks (a stale "guitar project", a "hackathon demo submission"
deadline). To clear everything and start from your own data:

```bash
psql -d digital_twin -c "TRUNCATE projects, deadlines, embeddings, nudges, conversation_history RESTART IDENTITY CASCADE;"
```

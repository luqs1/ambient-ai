# Ambient

Proactive agents, without the surveillance. A tray app for macOS, Windows and Linux that lets you
write triggers in plain words ("when someone asks me for a file", "whenever I create a calendar
event") and hands one matching event to the agent you chose. Everything that decides *whether* to
fire runs on your device by default: deterministic code plus two small local models. You can choose,
per trigger, to have the meaning check done by a zero-data-retention decision model instead (see
below). Nothing goes to your agent unless one of your triggers matches.

It does two jobs:

1. **Triggers.** Reads what your apps do, sorts it into a small catalog of "kinds of things that
   happen", and fires your triggers on them.
2. **Veil** (code: `engine/shield.py`). Detects and redacts common secrets in tool outputs before
   they reach an LLM provider, and restores them locally in the model's replies.

## How it reads any app

The network layer has no per-app code: an app it has never seen gets kinds the first time it does
something. (Two things in it do know vendors by name: the list of analytics hosts treated as background,
and a table of nicer names for well-known sites.) Three layers, most general first:

| Layer | What it does | Code |
|---|---|---|
| **Network, generic** | Local capture decrypts each app's traffic on the device. Every request and WebSocket frame is reduced to a signature (app, method, path template, operation name) and the human text inside it. Rules sort signatures into *actions* (you did something), *incoming* (something reached you), quiet *signals*, and background chatter (counted, never turned into an event). Browser traffic is attributed to the site, so Gmail in Zen is "Gmail". | `engine/events.py`, `engine/wsdecode.py` |
| **Local storage** (macOS only so far) | For apps whose traffic is end-to-end encrypted or pinned (WhatsApp), or that sync through system daemons (Reminders, Calendar, Notes, Messages, Mail), network activity is the cue to read what the app wrote to its own database: new rows with human text become events. Seen working live: WhatsApp messages you send, and Reminders. The other four are wired the same way but have not been seen producing events yet. | `engine/localstore.py` |
| **Curated** | Optional recognizers that add who/where for popular chat apps (Slack, Discord, WhatsApp, Granola). An upgrade, not a requirement. | `engine/recognizers.py` |

The **Apps** tab lists each connected app; open one to see: each kind, how often it happens, and a recent
example. Background chatter is hidden, so you see what you could trigger on, not a firehose.

## Triggers

A trigger is one sentence, the kinds of event it's bound to, and what your agent should do.

- **Write it in your own words.** A local embedding model ranks the catalog against your sentence
  and ticks the likely kinds; when it isn't sure it ticks nothing and says so. Two built-in groups
  exist: "your chat apps" (Slack, Discord and WhatsApp, the apps with curated recognizers) and "any
  website" (every site's page-opens, narrowed by the meaning check).
- **Show me once.** If the right kind isn't listed, press it and do the action in the app. Ambient
  lists what it just saw, and you tick the one you meant. This works for apps it has never seen.
  What you showed is one example (starring one repository); the trigger follows the kind once more
  examples show which parts of the path are names. The second is enough when they are spelled like
  names; plain words need more, so that two different operations are never merged on a guess.
- **Meaning check, on device or zero retention.** When the sentence is about content ("…asks me
  for a file", "…a pull request"), each event is judged against it. By default a small local
  classifier does this on your device. Per trigger you can instead choose Jev, TypeSafe's decision
  model, which is more accurate; it is sent only the fields that trigger names, never for a paused
  app and never sign-in or payment content, and it is only offered as zero retention when your
  TypeSafe account has zero-data-retention terms. Every check says where it ran. If the chosen
  checker can't run, the trigger doesn't fire. The builder backtests on recent events so you see
  what would have fired. Design: `docs/architecture-triggers.md`.
- **Ask me first / Automatic.** Automatic triggers are always visible: a chip in the header, a
  badge on the card, fire counts, and "ran on its own" on every result.
- **Agents.** Claude Code, Codex, opencode, or a local model through Ollama, per trigger. A trigger
  can only name one of these; it can never carry a command to run.

Only events that match a trigger become **moments** in the feed. Everything else stays in the
local catalog.

## Privacy rules built in

- Sign-in, banking, payment and password-manager hosts are never decrypted (`PRIVATE_HOSTS`).
- Sign-in and payment requests are never turned into events, and their bodies are never stored.
- Secrets in event text are masked before storage.
- Any app can be paused from the header or its Activity card; a paused app's content isn't kept.
- No server, no telemetry. The models ship inside the app, so nothing downloads on first run.
- The UI and API listen on 127.0.0.1 only, answer only requests addressed to that address (so a web
  page can't reach them by pointing a hostname at your machine), and refuse cross-origin requests.
- Raw traffic, kept for the Details view, is deleted after 3 days (`AMBIENT_FLOW_DAYS`). Sign-in
  headers, cookies and tokens in URLs are masked before anything is stored, and the database file is
  readable only by you.
- Traffic to model providers' own APIs (your prompts, their replies) never becomes an event.

Captured data is sensitive (message text, request bodies): it lives in the per-user support folder
(`~/Library/Application Support/Ambient`, `%LOCALAPPDATA%\Ambient`, `~/.local/share/ambient`).

## Run from source

```
uv sync
uv run python -m engine          # local capture of every app, UI on http://127.0.0.1:8787
# safe test, no system capture:
uv run python -m engine --mode regular@18083 --set listen_host=127.0.0.1 --set ambient_ui=18789 --set ambient_db=/tmp/a.db
```

Development with capture running: `scripts/live.sh start|stop|restart|status|ui` runs the engine
from a snapshot (so edits don't hot-reload it), with a watchdog that stops capture if the network
breaks. Triggers use a stand-in agent unless `AMBIENT_REAL_AGENT=1`.

```
HF_HUB_OFFLINE=1 AMBIENT_AGENT_CMD=cat uv run pytest -q        # unit tests, synthetic traffic only
uv run python tests/matrix_shapes.py                          # "any app" matrix: 30 app traffic shapes
tests/show_me_once.sh        # live: learn an unseen action, bind a trigger, fire it
tests/calendar_live.sh       # live: "whenever I create a calendar event in Google"
```

## Layout

| Path | What |
|---|---|
| `engine/addon.py` | mitmproxy addon: decide decrypt vs pass-through, record flows, feed events, Veil |
| `engine/events.py` | universal event layer: signatures, text extraction, classification, catalog, show-me-once |
| `engine/moments.py` | triggers, on-device meaning check, suggestions, previews, agent runs, MCP |
| `engine/decide.py` | local models (zero-shot classifier, embeddings) on onnxruntime |
| `engine/localstore.py` | local-storage fallback: WhatsApp adapter, generic SQLite reader |
| `engine/recognizers.py` | curated Slack / Discord / Granola recognizers |
| `engine/osal.py` | everything OS-specific: paths, processes, certificate trust, capture status, icons |
| `engine/ui.py`, `ui/` | local UI + JSON API + `/mcp`; `index.html` (window), `panel.html` (tray panel), `traffic.html` (details) |
| `app/` | Tauri tray shell: starts the engine sidecar, tray panel, notifications |
| `packaging/` | per-OS builds (`build-mac.sh`, `linux/`, `windows/`), test VM scripts (`vm/`) |
| `docs/` | `linux.md`, `windows.md`, `test-vms.md`, `legal-research.md`, `screenshots/` |

## Setup the app does once

Trust Ambient's certificate (one click, your OS confirms), allow capture (a network extension on
macOS, elevation on Windows, a capability or polkit prompt on Linux), and optionally let coding
agents trust the certificate so Veil can protect them. The window only shows the steps that still
need you. Call apps (Zoom, FaceTime, Teams…) are excluded because capture breaks WebRTC audio.

## Known limits

- Apps that pin certificates and keep no readable local store stay opaque (Signal, Telegram).
- The local-storage fallback is macOS only; Windows and Linux read the network only.
- Many sites so far only give "open a page": actions inside them appear once you do them (or use
  "show me once"), and a page served from the browser's cache makes no event.
- Linux captures browsers, Electron apps and common CLIs by default, not every process
  (`AMBIENT_CAPTURE=all` opts in); see `docs/linux.md`.
- The certificate and the Windows capture helper still carry mitmproxy's name in the prompts the OS
  shows, and the installers are unsigned.
- Electron apps that store data in LevelDB/IndexedDB aren't covered by the local-storage reader.
- Labels for unknown apps come from their API names, so some are rough ("sync", "save transactions").
- The meaning check is a small model: around nine in ten on clear cases. The backtest shows its
  scores so you can see mistakes before you save.
- Discord voice and browser calls may break while capture is on.

## License

[Functional Source License 1.1, Apache 2.0 future license](LICENSE.md) (FSL-1.1-ALv2), a
[fair source](https://fair.io) license: use, modify and self-host freely for anything except a
competing product; each version becomes Apache-2.0 two years after release.

# Overnight build report, 10 November 2026

Branch `nightly` of `luqs1/ambient-ai`. Built and tested between about 01:15 and 06:30. Nothing was
merged to `main`.

**Short version.** The three things asked for exist and run, but an independent review judged three
of them only partly met. Its most serious findings (a privacy hole in the local API, credentials in
stored traffic, a meaning check that silently switched itself off) are fixed and tested. What is
left is listed under "Still open" and is mostly breadth: few apps were actually exercised overnight.

## What was asked

1. Any application, not just Granola, Slack and Discord, should be open to reading real-time
   activity: by network where possible, otherwise using network activity as the cue to read the
   real data from local storage.
2. Replace the preset-list trigger builder with one that scales to any number of applications:
   find high-intent conditions, and let the user say in plain language what should happen.
3. Make it work on Windows and Linux too, testing on kismet-lianli.
4. Clear screenshots of each feature, and an independent assessment before stopping.

## The independent review

A separate agent was given the requests verbatim, the code, the tests, the screenshots and
read-only access to the running app, and told to find where the work falls short.

| Request                     | Its verdict                    | Main reasons                                                                                                                                                                                                                                                                                                    |
| --------------------------- | ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Any app                  | Partly met                     | The generic layer works live (GitHub CLI, Google Calendar, Reminders, WhatsApp sent messages), but 12 of the 19 apps seen overnight only offer "open a page", several have quiet kinds only, the local-storage fallback is macOS only, and Calendar, Notes, Messages and Mail readers have produced nothing yet |
| 2. Triggers without presets | Partly met                     | Sentence, backtest and show-me-once work; ranking was often wrong; the chat groups are still three named apps                                                                                                                                                                                                   |
| 3. Windows and Linux        | Partly met, unverifiable by it | Evidence was only example.com and curl; tested in VMs on kismet-lianli, not on the machine itself                                                                                                                                                                                                               |
| 4. Screenshots and review   | Mostly met                     | Gaps listed below                                                                                                                                                                                                                                                                                               |
| Complete for the Mac        | Partly met                     | The DMG predated most of the night's commits; packaged capture and native notifications unverified                                                                                                                                                                                                              |

### What it found, and what was done

| Finding                                                                                                                                                   | State                                                                                                                                                        |
| --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A sentence that names an app ("when someone on WhatsApp asks me for money") dropped the content condition, so the trigger would fire on every WhatsApp DM | **Fixed.** The check is on whenever the sentence says something about the content. `mac-08`                                                                  |
| A web page could reach the local API by DNS rebinding and read captured traffic; a trigger's `agent` field was run as a command                           | **Fixed.** Requests must be addressed to 127.0.0.1/localhost on the right port, cross-origin requests are refused, and a trigger can only name a known agent |
| Raw traffic kept forever, including `Authorization` and `Cookie` headers; database world-readable                                                         | **Fixed.** Credentials are masked before storing, existing rows were scrubbed, raw traffic is deleted after 3 days, the database is owner-only               |
| Five "[night test]" triggers left on and automatic                                                                                                        | **Fixed.** All switched off                                                                                                                                  |
| Wrong ticks: "I create a calendar event" ticked "open a page"; "I star a repository" ticked "repository create"; "I send an email" ticked chat apps       | **Fixed.** `mac-09`, `mac-10`. When unsure the builder now ticks nothing and offers "show me once"                                                           |
| "Any chat app" meant Slack, Discord and WhatsApp                                                                                                          | **Reworded** to "your chat apps", naming the three. Not made general (see Still open)                                                                        |
| A trigger shown one repo never fired for another                                                                                                          | **Fixed.** The trigger follows the kind from the second example. Verified live by starring a second repo                                                     |
| Prompts to model providers became events                                                                                                                  | **Fixed** for HTTP, SSE and WebSocket; 28 such kinds were removed from the live catalog                                                                      |
| Unreadable labels ("Sync"), x.ai shown as "X", analytics calls shown as actions                                                                           | **Fixed** ("Save a change", "xAI", re-sorted on load). Some rough labels remain                                                                              |
| Header said "Listening" with the certificate untrusted; tray panel showed raw `**bold**`                                                                  | **Fixed**                                                                                                                                                    |
| OS prompts say "mitmproxy" and "Publisher: Unknown"                                                                                                       | Open                                                                                                                                                         |
| The meaning check scored a repo home page 83% as a pull request                                                                                           | Open: it is a small model. The backtest shows such mistakes before saving                                                         |

### Second pass

A second independent agent then re-checked the fixes on the running engine. Its probes passed for
the content condition, the local API, the agent allow-list, masked headers and retention, provider
traffic, the five named ranking cases and the night-test triggers. It also found more:

| Finding | State |
|---|---|
| 48 request and reply bodies on sign-in paths, stored before the fix, were still on disk (some with real tokens) | **Fixed.** Scrubbed; a read-only count now finds 0 of 451 such rows holding a body |
| The new "learn from the second example" rule merged different operations (`/api/me/status` with `/api/me/avatar`) and fired the trigger on both | **Fixed.** The second example is only enough when the values are spelled like names; plain words wait for more evidence |
| Eight neighbouring sentences ticked a wrong kind with confidence ("I unstar…", "I post a tweet", "I join a Discord server", "someone messages me on Telegram"…) | **Fixed** for those; all now tick nothing or the right kind. Expect more such cases: it is a heuristic over a small model |
| Rebuilding the catalog from stored traffic ignored the provider exclusion; the UI could be framed by another page | **Fixed** |
| "The database stops growing now" was wrong | Corrected below |

Its verdicts after the first round of fixes: any app, partly met; plain-words triggers, partly met
and close to mostly; Windows and Linux, mostly met on the evidence (it could not check the VMs).
These last fixes were not reviewed a third time.

## 1. Any application

| Piece | State | Evidence |
|---|---|---|
| Generic network layer (`engine/events.py`): signatures, text extraction, sorting into actions / incoming / quiet / background | Built, live on the Mac | Live: Slack, Discord, GitHub web and CLI, Google Calendar, Google Search. `mac-13-app-detail.png` |
| Synthetic matrix of 30 app traffic shapes | 29 pass | `tests/matrix_shapes.py`. The shapes are hand-written from public API documentation, not recorded, and the rules were tuned against them: treat it as a regression test, not as proof for those apps. The one failure is Telegram Web (encrypts inside the socket) |
| Learned path names, so one kind covers every repo or workspace | Built, live | `tests/test_path_names.py`, `tests/star_follows_kind_live.sh` |
| Local-storage fallback (`engine/localstore.py`), macOS only | Built, live for two apps | WhatsApp: a message sent from the phone was read from WhatsApp's database. Reminders: a typed reminder appeared through the generic SQLite reader. `mac-06` |
| Activity tab with per-app pause | Built | `mac-13` (one app's page, with pause), `linux-03`, `windows-03` |

## 2. Triggers in plain words

| Piece | State | Evidence |
|---|---|---|
| One sentence, matched to catalog kinds by a local embedding model | Built | `mac-03`, `mac-08` to `mac-10`, `tests/test_review_fixes.py` |
| "Show me once" | Built, live | Starring a repo (`tests/show_me_once.sh`), saving a Google Calendar event (`tests/calendar_live.sh`). `mac-07` |
| On-device meaning check with backtest and strictness slider | Built, live | Promise note 99%, control under 1%; pull-request page 98%, issues page 4%. `mac-04`, `linux-09`, `windows-14` |
| "Any website" group | Built | "I open a pull request" binds to every site's page opens, narrowed by the check. `linux-09`, `windows-14` |
| Free-text action, per-trigger agent | Built | One real Claude Code run on a test issue (`mac-01`); everything else used a stand-in agent |
| Triggers always visible | Built | `mac-01`, `mac-05` |

The check runs on a 70 MB local classifier. Two larger local models scored no better (54 of 61
against 53 of 61 on `tests/eval_judge.py`), so the small one stays.

## 3. Windows and Linux

Built and tested in disposable VMs on kismet-lianli (`docs/test-vms.md`), not on the Windows machine
itself: its certificate store, drivers and security settings were not touched.

| | Linux (Ubuntu 24.04) | Windows 11 |
|---|---|---|
| Package | `.deb` | NSIS installer and MSI |
| Capture seen from | curl, Firefox, Chrome, Chromium | curl, Edge, Firefox; VS Code produced only telemetry kinds |
| Trigger fired (exact and meaning-checked) | Yes | Yes |
| Tray, panel, window, notification | Yes (left click opens the menu, not the panel) | Yes |
| Quit releases the network | Yes | Yes (Edge showed "closed unexpectedly" afterwards, `windows-10`) |
| Docs | `docs/linux.md` | `docs/windows.md` |

**Rebuilt from the final code.** After the review fixes, both were rebuilt in the VMs from commit
`1302917`, installed over the first builds and checked again with real pages (public GitHub pull
requests, no sign-in), which the first round had not done:

| Check on the installed app | Linux | Windows |
|---|---|---|
| Header "Listening"; "Needs setup" when the certificate is untrusted | Yes, both seen | "Listening" seen |
| Hostile `Host` header refused (403) | Yes | Yes |
| Database owner-only | Yes (0600; the old build's 0644 was tightened) | Not applicable |
| GitHub pages appear in Activity with their titles | Firefox, `linux-08` | Edge, `windows-13` |
| "I open a pull request" ticks "any website" with the check on | Yes, `linux-09` | Yes, `windows-14` |
| Trigger fires on a pull request, not on other pages | 96% against 4% and 0.1%; `linux-11` | 95% and 97% against 12%, 0.1% and 21%; `windows-16` |
| "Show me once" lists what was just done | A search, `linux-10` | A search, `windows-15` |
| Quit leaves nothing running and the network working | Yes | Yes (the WinDivert driver stays loaded until restart) |

Installers on this Mac, in `dist-vm/` (not in git), with checksums in `docs/linux.md` and
`docs/windows.md`: `Ambient_0.1.0_amd64.deb` (165 MB), `Ambient_0.1.0_x64-setup.exe` (129 MB),
`Ambient_0.1.0_x64_en-US.msi` (148 MB). All three match the checksums taken in the VMs. They were
built from `1302917`, so they lack the second-pass fixes above (commit `a6790ad`); rebuilding is one
command in each VM (`docs/linux.md`, `docs/windows.md`). The Windows DNS stall did not occur in this run's three capture starts.
Not repeated: Chrome on Linux, Firefox on Windows, installing the MSI, uninstalling.

Seen in these runs and not fixed: "show me once" can list a browser's own background requests next
to what you did ("Request · Fastly-edge" on Linux; eleven unreadable rows such as "T ias" and "Post"
from a search site's telemetry on Windows, `windows-15`); a search on one site was labelled "Item
dev" and appeared twice; a redirect page counts as "open a page".

## Tests

- `uv run pytest -q`: 902 pass, 1 expected failure (Telegram Web). Synthetic traffic only.
- A first run of the suite found 63 defects, including privacy leaks; all but Telegram are fixed.
- Live scripts in `tests/`: `show_me_once.sh`, `calendar_live.sh`, `real_agent_once.sh`,
  `star_follows_kind_live.sh`.

## Screenshots (`docs/screenshots/`)

| Feature | File |
|---|---|
| Moments and trigger cards | `mac-01` |
| Activity, with pause and local-storage badge | `mac-13` (the Activity tab is now the Apps tab) |
| Builder: sentence to kinds | `mac-03`, `mac-09` |
| Meaning check and backtest | `mac-04`, `mac-08` |
| Not sure, so nothing ticked | `mac-10` |
| Menu-bar panel | `mac-05` |
| Local-storage fallback | `mac-06` |
| Show me once | `mac-07` |
| Linux | `linux-01` to `linux-11` |
| Windows | `windows-01` to `windows-16` |

`mac-08` to `mac-10` and `mac-13` are the app's own page rendered in a headless browser, because the
Mac's screen locked at about 04:00 and window captures were no longer possible; they have no window
frame and use the light theme. There is no screenshot of installing on the Mac, the Mac's setup steps, or a
native Mac notification.

## Still open

- **Breadth.** Most sites only give "open a page" until you do something in them. Nobody used the
  Mac overnight, so incoming WhatsApp, Granola, VS Code, Google Docs and the Calendar, Notes,
  Messages and Mail readers have not been seen producing useful events.
- **Chat groups are three apps.** Another chat app works by naming it or by "show me once", not
  through "your chat apps".
- **"Open a pull request"** binds to viewing a pull-request page, not to creating one.
- **Slack shows the same send twice** ("I send a message" and "Chat post message").
- **Mac:** `dist/Ambient.dmg` (137 MB, unsigned) was rebuilt from the final code (`a6790ad`) at 06:38
  and its bundled engine checked without capture (UI, hardened API, local models, proxying). Capture
  from the packaged app is untested: a rebuilt app needs its network extension and file access
  approved again. Native notifications are implemented; none arrived in testing.
- **Windows:** DNS lookups stalled for about 12 s after capture started in about half of the first
  round's starts (also with plain mitmproxy) and in none of the second round's three; the VM froze
  once; elevation is asked on every start; unsigned.
- **Linux:** the default captures browsers, Electron apps and common CLIs, not every process
  (`AMBIENT_CAPTURE=all` opts in); the admin-password path was shown but not typed into.
- **Both:** networks that advertise IPv6 with no route hang apps under capture; the certificate and
  helper carry mitmproxy's name in OS prompts.
- **Veil:** the samples it keeps of a detected secret still show its first 6 and last 4 characters.
- The database on this Mac is about 210 MB after 12 hours of capture. Nothing is deleted until
  traffic is 3 days old, so it will grow for another two and a half days and then level off.
- The sign-in test is a list of path words and hosts; a sign-in endpoint with an unusual name would
  still have its body stored for 3 days.

## Left on your accounts by the tests (all safe to delete)

- Google Calendar: three events at 3am on Saturday 14 November, titled "Ambient night test …".
- Reminders: "Ambient night test reminder (safe to delete)".
- Slack, your own "message yourself" space: four "Ambient night test" notes.
- GitHub `luqs1/ambient-ai`: issues 4 to 8, all closed. One of your other repos was starred and
  unstarred twice.
- Ambient: five triggers named "[night test] …", all off. Your own three are untouched and off.

## One interruption

At 06:19 the watchdog stopped capture on this Mac after three failed internet checks in a row.
Connections to servers had been failing since 06:17, and the link to kismet-lianli (which does not
go through Ambient) dropped at the same time, so this looks like a network outage and not something
Ambient caused, but that is not proven. The network was fine when checked at 06:25 and capture was
started again then.

## Left running

- Live capture on this Mac (`scripts/live.sh stop` ends it), with a stand-in agent.
- The debug Ambient app.
- Both test VMs on kismet-lianli (`packaging/vm/vm stop win`, `packaging/vm/vm stop lin`).
- Copies of the installers in `C:\Users\Luqmaan\ambient-dist` on kismet-lianli (plain files).

## Decisions made without you

- A trigger taught by one example follows the whole kind once the path's name positions are learned
  (star one repo, fire on any). The earlier behaviour kept it on that one repo.
- Raw traffic is kept 3 days. Rarely used actions stay in the catalog 90 days; quiet kinds 14.
- Traffic to model providers' APIs never becomes an event.
- Sign-in, banking and password-manager hosts are never decrypted (a Bitwarden browser extension's
  traffic was being read before this; those rows were deleted).
- Linux captures a default list of programs, not everything.
- onnxruntime's telemetry is switched off.
- Website icons come from the browsers' own local favicon stores; nothing is fetched.

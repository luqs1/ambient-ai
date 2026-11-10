# Ambient — agent notes

- Scope: triggers on any app's activity + Veil (secret redaction; code name `shield`). Not a network analyser; raw traffic stays behind "details".
- No per-app code in `engine/events.py`. If an app doesn't work, fix the general rule (signature, text extraction, classification) and add its shape to `tests/matrix_shapes.py`. Curated extras go in `engine/recognizers.py`; OS specifics only in `engine/osal.py`.
- Whether a trigger fires is decided on the device (deterministic match + local models), or, only if the user picked it for that trigger, by a zero-data-retention decision model (engine/deciders.py) that receives just the fields the trigger names. Never send events to a general cloud LLM to decide. If the local model is unavailable, triggers with a meaning check don't fire.
- Triggers must stay visible: anything that can run on its own shows in the header chip, on its card, and on each result.
- Never start local capture while another engine runs (`pgrep -fl -- "-m engine|ambient-engine"`). Use `scripts/live.sh` (snapshot + watchdog); stop with SIGINT. Test in `--mode regular@PORT`, which never touches other apps' data.
- mitmproxy hot-reloads `engine/addon.py` when it changes and the reload drops the UI server. `scripts/live.sh` runs from a snapshot for this reason; `scripts/live.sh ui` syncs UI files without a restart.
- Never break a running agent session: agent CLIs are only decrypted when their env trusts our CA.
- A connection dropped mid-handshake is not proof an app distrusts the CA (see `SOFT_ERRORS` in addon.py). Don't blacklist apps on it.
- Sign-in, banking and password-manager hosts are never decrypted (`PRIVATE_HOSTS`); sign-in and payment requests never become events.
- Tests use synthetic traffic only. Don't read the user's databases in tests. `tests/*_live.sh` drive real apps and only send to the user themself.
- Veil SSE stream callables must return `[]`, not `b""` (empty chunk ends the response).
- Repo instructions go in this file, never CLAUDE.md.

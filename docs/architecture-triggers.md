# Triggers: wide events, programs, and where decisions run

Code: `engine/events.py` (wide events), `engine/triggers.py` (programs), `engine/deciders.py` (providers),
`engine/moments.py` (compile, run, backtest), `engine/triggers_api.py` (routes). Eval: `tests/eval_triggers.py`.

## 1. Wide events

Every stored event (action / incoming / signal; background and reads are only counted) keeps one flat,
typed record, like a canonical log line. `events.wide` holds the stored part; `wide_view(row)` adds the
row's own columns. `/api/events` rows gain a `wide` object.

| Group | Fields |
|---|---|
| core | `id ts app kind label klass direction via source transport` (`http`/`ws`/`sse`/`curated`/`local`) |
| request | `host method path path_template status op`, `path.<segment>` for learned ids/names (`path.repo`, `path.issue`), `query.<name>` |
| people, place | `who place recipients recipients.count channel` |
| content | `title text text_len urls` |
| body | `fields.<dotted.key>` from the request or frame, `resp.<dotted.key>` from a JSON reply; lists give `….count` and `…[].key` ("a@x, b@y"); keys that are data become `*` |

Bounds: 60 keys, 160 chars per string, 2 KB per row; body keys at most 36. Credential-named keys
(token, password, secret, cvv…) are dropped, every string goes through `mask_secrets`, GraphQL documents
and opaque blobs are skipped, and sign-in/payment exchanges never become events. Paused apps store
nothing. The table stays at the last 20 000 events (≈1 KB of wide data each).
Each kind learns a **schema** (`event_types.schema`: field → type, count, latest short example);
`GET /api/schema` returns the catalog as schemas.

## 2. Trigger programs

```json
{"v": 1, "when": "a customer emails about a refund",
 "match": {"kinds": ["Gmail|WS↓ mail.google.com#new_message"], "apps": [],
           "where": [{"field": "direction", "op": "eq", "value": "in"}], "any": []},
 "check": {"condition": "the email asks for a refund", "threshold": 0.7, "run": "zdr",
           "fields": ["app", "label", "direction", "who", "title", "text"]},
 "do": {"action": "ask", "instruction": "Draft a reply and file a ticket.",
        "steps": ["draft a reply", "file a ticket"]},
 "explain": "Fires on … if “the email asks for a refund” holds (checked by Jev, zero data retention, at 70% or more); then your agent will draft a reply and file a ticket."}
```

- `match` is deterministic and cheap. `kinds` are globs (empty = any kind, so kinds never seen still match);
  `apps` may name an app not seen yet. Ops: `eq ne in nin contains not_contains matches exists missing
  gt gte lt lte`; a field may use `*` (`fields.*attendees.count`). Short form: `"direction=out"`,
  `"op~create"`, `"fields.event.attendees.count>0"`, `"text contains invoice"`, `"who exists"`.
- `check` is optional: one natural-language condition for the semantic part, judged only for events
  that passed `match`. `fields` is exactly what the checker is shown.
- `explain` is regenerated from the program every time; it is the sentence the UI shows back.
- One prompt: `compile("whenever …, draft a reply and file a ticket")` splits *when* and *do*. Structured
  editing: send the edited program to `/api/triggers/explain` (normalize + sentence) and `/api/triggers/preview`.
- Storage: `triggers.program` (JSON) and `triggers.check_on`. `sigs` still holds the kinds (so path
  re-folding and pruning keep working). Triggers without a program behave exactly as before.

**Compile.** `compile(text, using, check_on)` gathers evidence and code assembles the program:
on the device, the existing embedding ranking and rules (`suggest`); with Jev, one System One request
asks per sentence: a Choice for direction (in/out/either), a Noul "is it about content", a Noul per
candidate kind and per app, and a Noul per candidate count condition. Thresholds live in code
(kinds ≥ 0.6 within 0.25 of the best; field conditions ≥ 0.7 and only fields the chosen kinds carry).
No kind fits → app/direction filters plus a check. Show-me-once still yields kinds to add to `match.kinds`.

## 3. Providers (`deciders.py`)

| | `device` (default) | `zdr` |
|---|---|---|
| model | DeBERTa-v3-xsmall NLI + bge-small, ONNX, CPU | TypeSafe Jev (`jev-latest`) |
| judge | `judge(state, condition) -> Judgment(p, reason, run, by, error)` | same; one request per event, all of its triggers' conditions as Nouls |
| compile | ranking + rules | typed answers above |
| sees, judging | everything stays here | only `check.fields` of events that passed `match`; never a paused app |
| sees, compiling | — | the sentence; kinds as app, label, method/host and plain path words, field names, examples masked to `<email>`, `<text, 4 words>`, `<number>` (switch: `compile_examples`) |

Key: `AMBIENT_TYPESAFE_KEY` / `TYPESAFE_API_KEY`, or the setting `zdr_key_file` (a .env path). Never
stored, logged or returned. Settings: `zdr_fallback` (`closed` | `device`), `compile_with`
(`device` | `zdr`; default zdr when a key exists), `compile_examples` (`masked` | `off`), `zdr_model`.
Never a general-purpose cloud LLM. Backtests on Jev send at most 20 recent events, in one request.

**Failure.** Jev unreachable or no key: the check fails closed (no moment, `checks` ring records `error`),
unless `zdr_fallback=device`, then it is judged here and labelled "checked on this device (Jev
unreachable)". Compile with Jev unreachable falls back to the device and says so in `note`.

## 4. What the UI must show

- On each trigger: `explain`, and `checked_by` ("checked on this device" / "checked by Jev, zero data
  retention"); a picker for `check_on` (default: this device). Show `warning` from save.
- On each result: `gate[].by` and `gate[].reason`; on errors, `gate[].error`.
- On writing: `written_by_label` ("written on this device" / "written with Jev, zero data retention"),
  the program as editable rows (kinds, apps, conditions, check, steps), `preview` counts and samples.
- Settings: `/api/deciders` (provider availability, fallback, compile switches, usage counters).

API (additive): `GET /api/schema`, `GET|POST /api/deciders`, `POST /api/triggers/compile {text, using?,
check_on?, preview?}`, `POST /api/triggers/explain {program}`; `POST /api/triggers` and
`/api/triggers/preview` accept `program` and `check_on`; `/api/triggers` rows gain `program, explain,
check_on, checked_by`; `/api/checks` rows gain `run, by, reason, error`; `/api/events` rows gain `wide`.

## 5. Numbers (tests/eval_triggers.py, 40 intents × 29 synthetic events)

| | precision | recall | F1 | intents exact |
|---|---|---|---|---|
| judge, device | 0.43 | 0.49 | balanced acc 0.73 | |
| judge, Jev | 0.84 | 0.92 | balanced acc 0.95 | |
| old: suggest + local check | 0.92 | 0.54 | 0.68 | 19/40 |
| new, written device, checked device | 0.84 | 0.69 | 0.76 | 23/40 |
| new, written device, checked Jev | 0.97 | 0.92 | 0.94 | 34/40 |
| new, written Jev, checked device | 0.96 | 0.79 | 0.87 | 29/40 |
| new, written Jev, checked Jev | 0.97 | 0.93 | 0.95 | 35/40 |

Device judge ≈9 ms per pair; Jev ≈200 ms per request of 40 questions.

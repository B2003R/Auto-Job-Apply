# Job Apply Agent

A typed Python agent that drives a **dedicated, headed Chrome profile**
containing your already-installed Jobright Autofill extension, stages job
applications from LinkedIn, Jobright, Wellfound, and Handshake, fills only
the gaps it is allowed to fill, and then **stops and waits for you** before
anything is submitted.

Two properties are the point of the whole project, and everything below
follows from them:

1. **Nothing is submitted without a human decision.** `AUTO_SUBMIT` is off
   by default, and even when it is on the approval gate still runs whenever
   a protected question, an unanswered gap, an unwritten answer, an
   incompletely scanned page, or a page that never settled is involved.
2. **Nothing is invented about you.** Visa/sponsorship, EEO/demographic,
   compensation, and employment-date questions are never answered by a
   model. They come from your own answers file or from you, at the gate.

---

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [The dedicated Chrome profile](#the-dedicated-chrome-profile)
- [The extension](#the-extension)
- [Configuration](#configuration)
- [Your canonical answers](#your-canonical-answers)
- [Preflight: `doctor`](#preflight-doctor)
- [Calibrating the toolbar click](#calibrating-the-toolbar-click)
- [Running the control plane](#running-the-control-plane)
- [The command line](#the-command-line)
- [Approving and rejecting](#approving-and-rejecting)
- [The HTTP API](#the-http-api)
- [Exporting the log](#exporting-the-log)
- [Rate limits](#rate-limits)
- [Privacy and what is stored](#privacy-and-what-is-stored)
- [Board adapters and selector maps](#board-adapters-and-selector-maps)
- [Questions the agent will not answer](#questions-the-agent-will-not-answer)
- [Recovery: what happens when something breaks](#recovery-what-happens-when-something-breaks)
- [Testing offline](#testing-offline)
- [Running without a display (`xvfb-run`)](#running-without-a-display-xvfb-run)
- [What is deliberately not implemented](#what-is-deliberately-not-implemented)

---

## Requirements

- Python 3.11 or newer.
- Linux with an X display for the headed browser. macOS and Windows are
  untested; the native toolbar-click fallback is Linux/`xdotool` only.
- Google Chrome (branded) with the Jobright Autofill extension already
  installed in a profile you keep for this purpose.
- Optional: `xdotool`, only for the fourth autofill trigger tier.
- Optional: an OpenAI-compatible endpoint, only for drafting free-text
  answers. Leaving `OPENAI_API_KEY` empty is fully supported — every
  free-text gap is then routed to you instead.

## Installation

```bash
git clone <this repository>
cd job-apply-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # installs this package plus dev extras
cp .env.example .env                     # then edit it
cp answers.example.yaml answers.yaml     # then edit it
```

`requirements.txt` is `-e .[dev]`, so the package is installed in editable
mode with the test dependencies. For a runtime-only install use
`pip install .`.

Playwright ships in the dependency list but **no browser download is
needed**: the agent launches the Chrome you already have, named by
`CHROME_EXECUTABLE`, and the test suite never opens a browser at all. You
do not need `playwright install`.

## The dedicated Chrome profile

**Do not point this at your everyday browsing profile.** The agent takes an
exclusive lock on the profile directory, opens tabs in it, and types into
forms in it.

```bash
mkdir -p ~/.config/job-apply-chrome
google-chrome --user-data-dir="$HOME/.config/job-apply-chrome"
```

In that window: sign in to the job boards you want to apply through, and
install/sign in to the Jobright Autofill extension. Then close it — the
agent needs the profile to itself.

Two locks protect that:

- `.job-apply-lock.json`, written by this agent, carries the owning pid and
  host. It is **never removed automatically**, even when the owning process
  is clearly gone; `doctor` tells you whether it looks stale.
- Chrome's own `SingletonLock`/`SingletonSocket`/`SingletonCookie`. If any
  of these exist the agent refuses to start, because a second Chrome on one
  profile directory corrupts it. Close the other Chrome; these files are
  never deleted for you either.

The browser is always headed. A headless run cannot drive a Chrome
extension's toolbar or popup, and a form being filled in someone's name is
something you should be able to watch.

## The extension

Chrome 137 and later removed `--load-extension` support from branded
Chrome, so production **requires the extension to be already installed** in
the profile. The agent verifies this by reading the profile's `Preferences`
/ `Secure Preferences` files rather than by guessing, and reports the
difference between "not installed" and "installed but disabled".

Set `JOBRIGHT_EXTENSION_ID` to the extension's id, visible at
`chrome://extensions` with Developer mode on.

The autofill trigger tries four tiers, in order, and stops at the first one
that both acts *and* produces observable field changes:

| Tier | What it does | Needs |
|---|---|---|
| 1. `in_page` | clicks an in-page Autofill control found across same-origin frames and open shadow roots | nothing |
| 2. `extension_popup` | opens `chrome-extension://<id>/popup.html` and clicks its Autofill control | the extension id |
| 3. `service_worker` | asks the MV3 service worker to dispatch the extension action | a live service worker |
| 4. `native_toolbar` | clicks the calibrated toolbar pixel | `xdotool`, `DISPLAY`, calibration |

A tier that "succeeded" without changing any field is recorded as failed
and the next one is tried. All four failing raises `TriggerFailed`, which
skips that one listing.

## Configuration

Everything is environment-driven through `pydantic-settings`; see
`.env.example` for the full annotated list. The settings you are most
likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `CHROME_EXECUTABLE` | `/usr/bin/google-chrome` | the browser to launch |
| `CHROME_PROFILE_PATH` | `/home/user/.config/job-apply-chrome` | the dedicated profile |
| `JOBRIGHT_EXTENSION_ID` | *(placeholder)* | from `chrome://extensions` |
| `AUTO_SUBMIT` | `false` | **leave it off** unless you have read the gate rules below |
| `LOG_FIELD_VALUES` | `false` | whether answer text is stored at all |
| `ANSWERS_PATH` | `./answers.yaml` | your canonical answers |
| `SQLITE_PATH` | `./data/jobs.db` | queue, applications, approvals, rate events |
| `ARTIFACTS_PATH` | `./data/artifacts` | screenshots |
| `LINKEDIN_DAILY_CAP` and friends | `40` | per-board, per-UTC-day caps |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8765` | where the control plane binds |
| `API_TOKEN` | *(empty)* | bearer token; empty means loopback-only |
| `API_ACTOR` | *(empty)* | what a token holder is recorded as |
| `OPENAI_API_KEY` | *(empty)* | empty is supported: no model is called |

Numbers are validated at load. A negative cap or a zero model timeout stops
the process with the variable's name in the error rather than being
absorbed somewhere downstream.

## Your canonical answers

`answers.yaml` is where you write the answers you have already decided on.
Copy `answers.example.yaml` — every line in it is commented out on purpose,
so a straight copy answers nothing until you edit it.

Two shapes are accepted, and a file uses one or the other. The short one
maps a question to an answer:

```yaml
answers:
  "Preferred name": "Alex Kim"
  "How did you hear about us?": "Company careers page"
```

The long one is a list, for when one answer has to match several phrasings
or should be keyed to a form control's `name` rather than its label:

```yaml
answers:
  - question: "Why do you want to work here?"
    value: "I have followed this team's work on ..."
    aliases:
      - "What interests you about this role?"
      - "Why are you applying?"
  - question: "LinkedIn profile"
    value: "https://www.linkedin.com/in/example"
    names:
      - linkedin_url
```

Matching ignores case, surrounding whitespace, and trailing decoration, so
`Preferred name` also answers a page rendering `Preferred Name *`. A
question answered twice is an error, not a silent last-one-wins, and a
malformed file is rejected whole rather than partially applied.

**Quote everything.** YAML reads a bare `yes` as a boolean and `2024` as a
number; the loader refuses those rather than guessing what you meant to
type into someone's form.

This file is also **the only way a protected question gets filled without
stopping for you** — and even then, a protected question on the form still
forces the approval gate to run before submission.

`answers.yaml` is in `.gitignore`. Keep it that way.

## Preflight: `doctor`

```bash
python -m scripts.doctor
```

Reports each check as `OK`, `WARNING`, or `ERROR`, and exits non-zero if
anything is an error. It distinguishes, rather than lumping together:

- profile directory missing
- profile locked (by this agent, stale or live) or in use by Chrome itself
- extension missing, or installed but disabled
- service worker never discovered, versus discovered but unresponsive
- `xdotool` missing
- toolbar coordinates uncalibrated
- `DISPLAY` unset while the native tier is enabled
- zero or several Chrome windows matching `CHROME_WINDOW_NAME`

Run it before your first batch, and again whenever a run starts failing in
a way you do not recognise.

`doctor` briefly takes the profile lock itself to prove it can, and
releases it again. **Run it when the control plane is not running**, or the
lock check reports the running worker as the thing in the way — which is
true, but is not the answer you were looking for.

## Calibrating the toolbar click

Only needed if you want the fourth trigger tier. It clicks a fixed screen
pixel, so it refuses to run until you have measured one — the uncalibrated
default (`0, 0`) is a sentinel, not a coordinate.

```bash
python -m scripts.calibrate_toolbar          # hover the toolbar button during the countdown
```

Paste the printed `TOOLBAR_X` / `TOOLBAR_Y` lines into `.env`. Re-calibrate
whenever the window size, screen resolution, or toolbar contents change. If
several Chrome windows match `CHROME_WINDOW_NAME`, set `CHROME_WINDOW_ID`
(`xdotool search --name "Google Chrome"`) so the tier knows which one holds
the toolbar.

## Running the control plane

```bash
python -m app.main                 # binds API_HOST:API_PORT
python -m app.main --port 9000     # override for one run
```

One process, one browser, one worker. The FastAPI lifespan builds the
worker once, starts it once (which is what opens Chrome), and stops it
once. A second worker in the same process is refused rather than tolerated,
because two Playwright contexts on one Chrome profile is exactly what the
profile lock exists to prevent.

The worker claims pending queue items one at a time — the claim is a
compare-and-set inside a single transaction, so a second worker on the same
database cannot take the same listing — runs each through the application
graph, and parks it at the approval gate. A skipped or failed listing does
not stop the batch; the reason is recorded on the queue row and the next
item runs.

**Binding.** The default is loopback. With no `API_TOKEN` configured the
server serves loopback callers only, checked per request, and
`python -m app.main` **refuses to start** if `API_HOST` is anything wider.
Set `API_TOKEN` before exposing it, and remember what the API can do: it
can submit job applications in your name.

## The command line

`scripts/run_batch.py` talks to the control plane by default, because that
is the process holding the browser and the staged tab.

```bash
# queue some listings
python -m scripts.run_batch queue \
    https://www.linkedin.com/jobs/view/123456/ --board linkedin

# queue and watch until each one is ready for a decision
python -m scripts.run_batch run https://www.linkedin.com/jobs/view/123456/ \
    --board linkedin --timeout 300

# what is waiting for me?
python -m scripts.run_batch pending

# one run in detail, as JSON
python -m scripts.run_batch --json status 7
```

Global options: `--api URL` (default `http://API_HOST:API_PORT`),
`--token`, `--json`, and `--local`. `--api` and `--local` are mutually
exclusive — naming both would hide which one a run actually used.

### `--local`: an in-process worker

```bash
python -m scripts.run_batch --local run \
    https://www.linkedin.com/jobs/view/123456/ --board linkedin --prompt
```

No server. The CLI starts its own worker, drains the queue in this process,
stops the worker, and exits. With `--prompt` it asks you about each staged
application at the console, through the same approval service the API uses.
Without `--prompt` it stops at the gate like everything else.

`--local run` with no URLs drains whatever is already queued.

`approve`, `reject`, `status`, and `pending` need the control plane:
`--local` starts a worker for one batch and stops it again, so there is
nothing for a second command to talk to.

## Approving and rejecting

An application waiting at the gate is a filled form in a real tab. You
decide.

```bash
python -m scripts.run_batch approve 12 --note "good fit"
python -m scripts.run_batch reject 12 --note "wrong seniority"
```

Rules the gate enforces, wherever the decision comes from:

- **A decision names an application, never a thread.** There is no
  parameter with which to aim a decision at somebody else's application.
- **The actor is the authenticated caller.** An `actor` field in the
  request body is *refused*, not ignored, so nobody can attribute a
  submission to someone else. Over loopback the recorded actor is
  `loopback:127.0.0.1`; with a token it is `API_ACTOR`, or
  `api-token:<fingerprint>` when that is unset — a hash prefix that
  distinguishes two credentials without either being recoverable from the
  audit record. At a `--local --prompt` console it is `--actor`, or
  `console:<username>`.
- **Repeating the identical decision is a no-op** that returns the original
  record, including its original timestamp. Retry freely.
- **Anything else is a conflict (409).** A different verdict, a different
  person, or a different note is a new statement, and the first record is
  never overwritten.
- **A thread another worker is running is 423**, with `Retry-After`. That
  is not a conflict — your request was simply early.

Approving runs the submit step **in the process that staged the tab**. This
is why approval over HTTP goes to the server and not to a fresh CLI
process: see [Recovery](#recovery-what-happens-when-something-breaks).

## The HTTP API

All routes require the loopback/bearer authentication described above.
Errors share one shape:

```json
{"error": {"kind": "approval_conflict", "message": "..."}}
```

| Route | Purpose |
|---|---|
| `GET /health` | is the worker up |
| `POST /queue` | `{"listing_url": ..., "board": "linkedin"}` → 201 |
| `GET /runs/{queue_id}` | queue row, application, decision, and the gate payload |
| `GET /applications?status=awaiting_approval` | everything at the gate |
| `POST /applications/{id}/approve` | `{"note": "..."}` (optional) |
| `POST /applications/{id}/reject` | `{"note": "..."}` (optional) |

Statuses:

| Status | When |
|---|---|
| 200 | decided, or already decided identically |
| 201 | queued |
| 401 | missing, wrong, or unexpected credentials |
| 403 | no token configured and the caller is not on loopback |
| 404 | no such run, application, or thread |
| 409 | the decision contradicts one already recorded, or the application is not at the gate |
| 422 | validation: an unknown field, a bad board, an over-long note, a listing URL that is not that board's |
| 423 | another worker holds this thread; `Retry-After` says when to come back |
| 503 | the worker is still starting |

An unknown field in a request body is a 422 rather than being ignored,
because a client that believes it is controlling something it is not is a
client about to be surprised.

Interactive docs are at `/docs` while the server is running.

## Exporting the log

```bash
python -m scripts.export_log --format json --output applications.json
python -m scripts.export_log --format csv --fields --status submitted
```

- `--format json` produces one nested document; `--format csv` produces one
  row per application, or one row per field with `--fields`.
- `--status` (repeatable) narrows by application status.
- `--include-values` includes answer text — **and only has an effect when
  `LOG_FIELD_VALUES` is enabled**. Both are required: the setting is the
  standing policy for the installation, the flag is you saying you mean it
  for this export. A database written while logging was on and exported
  after it was turned off stays redacted.

## Rate limits

Per board, per UTC day, counted from persisted events:
`LINKEDIN_DAILY_CAP`, `JOBRIGHT_DAILY_CAP`, `WELLFOUND_DAILY_CAP`,
`HANDSHAKE_DAILY_CAP`, all 40 by default.

**There is no runtime bypass.** Admission is a check and an insert inside
one transaction, so two workers that both see 39 of 40 cannot both be
admitted. A listing that arrives at a full board is skipped with
`rate_cap_reached` and can be queued again after the cap resets. `0` is a
valid cap and means "apply to nothing on this board today".

## Privacy and what is stored

The SQLite database holds the queue, application records, field
*provenance*, approvals, rate events, and execution leases. Alongside it,
`checkpoints.sqlite` holds the LangGraph checkpoints that make the approval
gate survive a restart.

What is **not** stored by default:

- **Answer text.** With `LOG_FIELD_VALUES=false` (the default), field rows
  record which control was filled, by whom (`jobright`, `llm`, or `user`),
  whether it was required, and whether it ended up filled — but not the
  value. The approval-gate payload written into the checkpoint honours the
  same flag, so what a reviewer is shown is the *question*, not the answer.
- **Your API key.** It is held as a `SecretStr`, so it cannot reach a log
  line, a `repr`, or a traceback by accident.
- **Your reviewer notes, in error messages.** A conflict response names the
  stored decision, the actor, and the timestamp — never the note.
- **Who approved what, in the checkpoint file.** The resume payload carries
  the decision and its timestamp only. The approvals table is the audit
  record; a checkpoint file gets copied around with a working directory.

What *is* sent off the machine: only free-text questions the gap filler is
allowed to send, and only when `OPENAI_API_KEY` is set. Protected
categories are refused at that boundary, so a visa or salary question is
never transmitted even if something upstream misclassified it.

`data/` and `answers.yaml` are gitignored. Screenshots under
`ARTIFACTS_PATH` are pictures of real application forms — treat that
directory as sensitive.

## Board adapters and selector maps

Each board has a small adapter and a YAML selector map in
`app/boards/selectors/`. **The YAML is meant to be edited**: boards change
their markup often, and nothing here needs a code change to follow along.
Maps are validated when the adapter is constructed, so a missing key or an
empty selector fails immediately, with the file named, rather than silently
matching nothing.

A listing URL is checked against the board it was queued as before any
navigation happens — an unrecognised host, a lookalike domain, or a
non-`http(s)` scheme is refused with the queue row untouched.

Two flows are deliberately unsupported and are skipped with their own
reasons:

| Flow | Reason |
|---|---|
| LinkedIn Easy Apply | `linkedin_easy_apply_unsupported` |
| Wellfound in-app apply | `wellfound_in_app_apply_unsupported` |

Both are multi-step in-page modals with no external ATS page for the rest
of the graph to detect or drive. Jobright also has a direct
Apply-with-Autofill path, which is supported.

## Questions the agent will not answer

The gap filler consults your `answers.yaml` first, and only then a model —
and only for questions that read as a request for **prose**: a cover
letter, a "why us?", an open-ended "tell us about ...". Everything else is
left to you. Specifically:

- **Protected categories are never sent to a model**: visa/sponsorship,
  EEO/demographics, compensation, employment dates. Classification looks at
  the label, the control name, and the id, and splits `camelCase` and
  `snake_case`, because ATS pages routinely leave the label blank and carry
  the meaning in `name="eeo_race"`.
- **Non-English and unrecognised labels go to you.** The prose allowlist is
  pattern-based and English. A question phrased in another language, or in
  a way the patterns do not recognise, is *not* guessed at — it becomes an
  unanswered gap, and the approval gate reports it. This is a deliberate
  bias toward asking rather than inventing; if you apply mostly in another
  language, expect to fill more fields by hand.
- **File uploads, signatures, and similar controls** always require a
  human.
- **A field that could not even be scanned** (a cross-origin frame, say) is
  reported as incomplete coverage, and blocks auto-submit on its own — an
  empty gap list from an incomplete scan is not the same as no gaps.

When a gap is left for you, the honest workflow is: open the staged tab,
type the answer yourself, then approve.

## Recovery: what happens when something breaks

| Situation | What happens | What you do |
|---|---|---|
| Unknown ATS after the Apply click | skipped, `unknown_ats` | apply by hand |
| Captcha or login wall | skipped, `captcha_required` / `login_required` | sign in yourself in the dedicated profile, queue it again |
| All four autofill tiers failed | skipped, `trigger_failed` | check `doctor`; re-calibrate |
| Board cap reached | skipped, `rate_cap_reached` | wait for the UTC day to roll over |
| Worker restarted mid-staging | skipped, `staged_page_lost` / `staging_artefacts_lost` | queue it again; nothing was submitted |
| Worker restarted after you approved | **failed**, and the reason says so | somebody approved a submission that did not happen; queue it again and decide again |
| Approved application whose checkpoint is gone | skipped, `stale_approval` | queue the listing again; the old decision is never replayed onto a freshly scanned form |
| A worker was killed holding a thread | its execution lease expires after two minutes, then another worker may take over | wait, or retry the decision |
| The runner itself fell over on one item | queue row `failed`, reason `worker_error`; the batch continues | read the traceback in the worker's log |

The consistent rule: **before a decision, a loss is a skip** (nothing was
submitted, so the listing can be staged afresh); **after a decision, a loss
is a failure** (somebody approved something that then did not happen, and
they should be told).

A queue item is a one-shot. Re-running a finished one reports its recorded
outcome instead of applying again — that is what stops a second Apply click
in your name — so genuinely re-applying means queueing the listing again.

If a process is killed outright, its profile lock file stays behind. Run
`doctor`, confirm the pid is gone, and remove `.job-apply-lock.json`
yourself.

## Testing offline

Everything except the browser integration tests runs with no browser, no
display, and no network:

```bash
pytest -q                                  # the whole suite
pytest tests/test_api.py -q                # the control plane
pytest tests/scripts/test_cli.py -q        # the CLIs
python -m mypy app scripts --ignore-missing-imports
```

The offline test system has three pieces:

- **Fixture ATS pages** in `tests/fixtures/ats/` (Greenhouse-, Lever-,
  Workday-, and unknown-like), served by a threaded HTTP server bound to
  `127.0.0.1` on an ephemeral port. It binds loopback only, on purpose.

  ```bash
  pytest tests/test_fixture_server.py -q
  ```

- **A stub MV3 extension** in `tests/fixtures/fake_extension/`. It injects
  an open shadow-DOM sidebar with an Autofill button, fills a subset of
  fields asynchronously, and **deliberately leaves one required input and
  one textarea empty**, so the gap-filling and approval paths are exercised
  rather than assumed. Its contract — what it fills, and what it leaves for
  a human — is pinned as a JSON fixture
  (`tests/fixtures/stub_gap_contract.json`) so the stub cannot drift into
  being easier to satisfy than a real extension.

  ```bash
  pytest tests/test_stub_extension_contract.py -q
  ```

- **Board fixtures**: adapters are tested against recorded HTML snippets,
  never against a live board.

**No test submits a real application, and no test launches a browser.**
There is no browser integration suite in this repository: every test runs
with no browser, no display, and no network, including the control-plane
and CLI tests, which drive the shipped worker, database, approval service,
rate limiter, gap filler, and LangGraph graph with only the
browser-touching parts faked. The stub extension is a contract fixture, not
something a test loads into Chrome.

The practical consequence is worth stating plainly: **the parts of this
system that touch a real browser are the parts the suite cannot vouch for.**
`doctor` is how you check those on your own machine.

## Running without a display (`xvfb-run`)

The browser is always headed, so a machine with no X display needs a
virtual one:

```bash
xvfb-run -a python -m app.main
xvfb-run -a python -m scripts.doctor
```

`pytest` needs none of this — the suite never opens a display, and running
it under `xvfb-run` changes nothing.

Two caveats for a virtual display. The native toolbar-click tier needs a
real or virtual display *and* calibration, and coordinates measured on your
physical screen will not be the right ones under `xvfb-run` — re-run
`scripts/calibrate_toolbar.py` inside the same virtual display, at the same
geometry, or leave that tier uncalibrated and let the first three tiers do
the work. And a virtual display is still a display: the browser is really
running, really logged in, and really filling forms, with nobody watching.

## What is deliberately not implemented

Being explicit, because these are the places where "it did nothing" could
otherwise be mistaken for "it worked":

- **There is no field writer and no submitter wired into this build.** Both
  are injected dependencies, and the shipped defaults *raise* rather than
  quietly no-op: an application that needs an answer typed, or that is
  approved and would be submitted, is recorded as **failed** with the
  missing component named. Everything up to and including the approval gate
  works end to end; the last click does not exist yet. This is why nothing
  in this repository can submit a real application today.
- **There is no captcha/login-wall detector wired in.** The graph handles
  both wherever they are raised, and the page-guard hook is there, but no
  production guard ships. Until one does, a challenged page is staged like
  any other and the approval gate is what stands between it and a
  submission.
- **`data/` is not pruned.** Screenshots and rows accumulate.

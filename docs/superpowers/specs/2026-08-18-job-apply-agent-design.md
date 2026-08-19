# Job Apply Agent Design

## Goal

Build a modular, typed Python agent that uses a headed Chrome persistent
profile containing the already-installed Jobright Autofill extension to stage
job applications from LinkedIn, Jobright, Wellfound, and Handshake. The agent
must stop at a durable human approval gate before submission by default and
must be testable offline with a stub extension and fake ATS pages.

## Browser constraints

- Production uses an existing extension in a dedicated Chrome profile. Chrome
  137 and later do not support `--load-extension` in branded Chrome.
- Offline tests may use `--load-extension` with Chromium or Chrome for Testing.
- Playwright's `--disable-extensions` and
  `--disable-component-extensions-with-background-pages` defaults must be
  removed when launching the persistent context.
- One long-lived process owns the browser context. A profile lock prevents a
  second process from opening the same user-data directory.
- The browser is always headed. The dedicated profile must not be the user's
  daily browsing profile.

## Architecture

`app/main.py` hosts a FastAPI control plane and a single worker task that owns
the persistent `BrowserContext`. SQLite stores the queue, application state,
field provenance, approvals, and rate events. Each queue item runs through a
LangGraph state machine with a SQLite checkpointer. The CLI batch runner talks
to the API, with an explicit local mode for an in-process worker.

The application graph is:

1. Open the listing through a board adapter.
2. Click Apply and detect the ATS.
3. Snapshot fields.
4. trigger Jobright Autofill.
5. Wait for mutation quiescence and snapshot again.
6. Attribute changed fields to Jobright and identify required gaps.
7. Fill safe gaps from canonical answers or a model.
8. Interrupt for approval unless auto-submit is explicitly enabled and no
   protected question remains.
9. Submit or reject, then persist the result and cost.

Unknown ATS layouts, captchas, login walls, unavailable extensions, failed
triggers, and reached rate caps become typed failures. They are logged with a
screenshot where possible and do not stop subsequent queue items.

## Components

### Browser and extension

`browser_session.py` builds persistent launch arguments, acquires and releases
an atomic profile lock, and owns Playwright lifecycle. `extension.py` reads
Chrome profile preferences to verify installation and discovers an extension
service worker. `scripts/doctor.py` reports actionable preflight failures.

### Autofill trigger and form scanner

`form_scanner.py` snapshots visible form controls across same-origin frames and
open shadow roots. Stable field keys combine frame URL, form/control identity,
name, type, and label. A diff reports changed fields, newly filled fields, and
still-empty required fields. Settle detection waits until DOM mutations and
field values remain unchanged for a configured quiet period.

`jobright_trigger.py` attempts four tiers in order:

1. Deep-query an in-page control whose accessible text matches `autofill`.
2. Open `chrome-extension://<id>/popup.html` and click its Autofill control.
3. Ask the MV3 service worker to dispatch the action/open the popup when that
   API is available.
4. Use Linux `xdotool` with explicitly calibrated toolbar coordinates.

It returns the successful tier and observed field diff. Tier failures are
retained for diagnostics; all tiers failing raises `TriggerFailed`.

### Safety and gap filling

`ats_detector.py` classifies Workday, Greenhouse, Lever, iCIMS,
SmartRecruiters, or unknown from URL and DOM markers. `gap_filler.py` checks
`answers.yaml` before a model. It never invents visa/sponsorship, EEO,
compensation, or employment-date answers; unresolved protected fields require
the approval gate. `model_router.py` deterministically selects routine or
escalation models and returns token/cost metadata. Field values are not stored
unless `LOG_FIELD_VALUES=true`.

`rate_limiter.py` counts persisted per-board events by UTC date. Defaults
include `LINKEDIN_DAILY_CAP=40`; there is no runtime override bypass.
`humanize.py` provides randomized, injectable delays and mouse paths.

### Boards, graph, and control plane

Board adapters implement a common typed protocol and read selectors from YAML.
LinkedIn Easy Apply and Wellfound in-app applications are skipped with a
specific reason. Jobright also supports its direct Apply-with-Autofill path.

The graph uses LangGraph `interrupt()` with a SQLite checkpointer. CLI and API
approval gates resume the same thread. FastAPI exposes queue creation, run
status, and approve/reject endpoints. Application logs include status, trigger
tier, field provenance, screenshots, timing, and model cost.

## Data model

- `job_queue`: listing URL, board, state, timestamps, and error reason.
- `applications`: queue reference, graph thread, ATS, status, trigger tier,
  model cost, screenshot, and timestamps.
- `application_fields`: application reference, stable key, metadata, source
  (`jobright`, `llm`, or `user`), required/filled flags, and optionally value.
- `approvals`: application reference, decision, actor, note, and timestamp.
- `rate_events`: board, action, and UTC timestamp.

SQLite migrations are idempotent and run at startup. Writes use explicit
transactions and foreign keys.

## Configuration

Configuration uses `pydantic-settings`. `.env.example` documents Chrome paths,
extension ID, toolbar coordinates, SQLite/artifact paths, auto-submit,
field-value logging, board caps, delays, model URLs/names/keys, and token
prices. Unsafe defaults are disabled: `AUTO_SUBMIT=false` and
`LOG_FIELD_VALUES=false`.

## Offline test system

The fixture server binds only to `127.0.0.1` and serves Greenhouse-, Lever-,
Workday-, and unknown-like pages. The MV3 stub extension injects an open
shadow-DOM sidebar and progress panel, asynchronously fills a subset of fields,
and deliberately leaves one required input and one textarea empty.

Unit tests cover launch argument construction, locking, extension preference
inspection, field snapshots/diffs, ATS detection, trigger fallback ordering,
rate limits, answer safety, model routing, and approval transitions.
Integration tests launch headed Chromium against the stub extension, verify
the selected trigger tier and mutation settling, and run a staged-then-approved
graph. Browser integration tests skip with an explicit reason if no browser or
display is available; `xvfb-run` is documented as the fallback.

Board parsing uses recorded HTML fixtures. No test submits a real application.

## Deliverables and acceptance

- The package is type-annotated and importable on supported Python versions.
- `pytest` proves all offline unit behavior; browser tests prove extension
  interaction when the local environment supports headed Chromium.
- `scripts/doctor.py` distinguishes missing profile, locked profile, missing
  extension, dead service worker, missing `xdotool`, and invalid calibration.
- A queued fake listing can reach a persisted approval interrupt and resume to
  a completed fake submission.
- README instructions cover installation, dedicated-profile setup, extension
  setup, fixture testing, adapters, approval, API/CLI use, and privacy.
- Real submissions remain impossible by default without an explicit approval.

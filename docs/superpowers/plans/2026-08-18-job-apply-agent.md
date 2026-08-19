# Job Apply Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a typed, offline-testable Python orchestrator that controls a persistent headed browser, invokes Jobright Autofill, fills only safe gaps, and durably pauses before submission.

**Architecture:** A FastAPI process owns one Playwright persistent context and feeds queue records into a LangGraph application loop. Focused browser, scanner, trigger, safety, board, and storage modules communicate through typed models; SQLite persists queue state, logs, rates, and approvals.

**Tech Stack:** Python 3.11+, Playwright, FastAPI, LangGraph, pydantic-settings, aiosqlite, PyYAML, httpx, pytest, pytest-asyncio.

## Global Constraints

- Production uses an already-installed extension in a dedicated headed Chrome profile.
- Tests alone may load an unpacked stub extension.
- Remove Playwright's two extension-disabling default switches.
- `AUTO_SUBMIT=false` and `LOG_FIELD_VALUES=false` by default.
- Never invent visa/sponsorship, EEO, compensation, or employment-date answers.
- No test or default command submits a real application.
- Rate caps have no runtime bypass.

---

### Task 1: Package foundation and persistence

**Files:**
- Create: `requirements.txt`, `.env.example`, `pyproject.toml`, `app/__init__.py`
- Create: `app/config.py`, `app/storage/models.py`, `app/storage/db.py`, `app/storage/logger.py`
- Test: `tests/test_config.py`, `tests/storage/test_db.py`

**Interfaces:**
- Produces: `Settings`, storage enums/dataclasses, `Database.initialize()`,
  queue/application/approval/rate methods, and `ApplicationLogger`.

- [ ] Write tests proving safe settings defaults, env parsing, idempotent schema
  creation, queue transitions, approval persistence, UTC rate counts, and
  redaction of field values.
- [ ] Run `pytest tests/test_config.py tests/storage -q`; verify imports or
  assertions fail because modules do not exist.
- [ ] Add the smallest typed configuration and SQLite implementations satisfying
  those tests.
- [ ] Re-run the focused tests and confirm they pass.
- [ ] Commit with `feat: scaffold job application storage`.

### Task 2: Offline ATS and extension fixtures

**Files:**
- Create: `tests/fixtures/ats/{greenhouse,lever,workday,unknown}.html`
- Create: `tests/fixtures/fake_extension/{manifest.json,background.js,content.js}`
- Create: `tests/fixture_server.py`, `tests/test_fixture_server.py`

**Interfaces:**
- Produces: `fixture_server` pytest fixture yielding a loopback base URL.
- Stub content script: open shadow root, `Autofill` button, progress status,
  asynchronous partial field fill, and `jobright-stub-complete` event.

- [ ] Write a test that fetches every ATS fixture over `127.0.0.1` and verifies
  its expected form markers.
- [ ] Run the test and verify it fails because the server/fixtures are absent.
- [ ] Add static fixtures, MV3 stub, and threaded loopback-only server.
- [ ] Re-run and confirm the fixture test passes.
- [ ] Commit with `test: add offline ATS and extension fixtures`.

### Task 3: Persistent browser and extension discovery

**Files:**
- Create: `app/agent/errors.py`, `app/agent/browser_session.py`,
  `app/agent/extension.py`, `scripts/doctor.py`
- Test: `tests/agent/test_browser_session.py`,
  `tests/agent/test_extension.py`, `tests/scripts/test_doctor.py`

**Interfaces:**
- `build_launch_options(settings: Settings) -> dict[str, object]`
- `ProfileLock(path: Path)` async/context manager
- `BrowserSession.start()/close()`
- `find_installed_extension(profile, extension_id) -> ExtensionInstall`
- `find_service_worker(context, extension_id, timeout_ms) -> Worker`

- [ ] Write tests for exact ignored default args, headed persistent options,
  atomic lock exclusion/release/stale diagnostics, both Chrome preference files,
  worker URL matching, and doctor error categories.
- [ ] Run focused tests and verify missing APIs fail.
- [ ] Implement launch option construction, lock lifecycle, lazy Playwright
  imports, extension discovery, and preflight reports.
- [ ] Re-run focused tests and confirm all pass.
- [ ] Commit with `feat: add persistent extension browser session`.

### Task 4: Deep form scanning and four-tier trigger

**Files:**
- Create: `app/agent/form_scanner.py`, `app/agent/native_click.py`,
  `app/agent/jobright_trigger.py`, `scripts/calibrate_toolbar.py`
- Test: `tests/agent/test_form_scanner.py`,
  `tests/agent/test_jobright_trigger.py`, `tests/agent/test_native_click.py`

**Interfaces:**
- `FormScanner.snapshot(page) -> FormSnapshot`
- `FormSnapshot.diff(after) -> FormDiff`
- `FormScanner.wait_for_settle(page, previous, quiet_ms, timeout_ms)`
- `JobrightTrigger.trigger(page, before) -> TriggerResult`
- `NativeToolbarClick.click()`

- [ ] Write scanner tests around a fake async page proving stable keys, required
  gap detection, changed/filled attribution, shadow/frame traversal script, and
  quiet-period settling.
- [ ] Run scanner tests and verify failure due to absent implementation.
- [ ] Implement immutable field/snapshot/diff models and browser evaluation.
- [ ] Re-run scanner tests until green.
- [ ] Write trigger tests proving tier order, fallback diagnostics, first-success
  short circuit, field diff return, and all-failed `TriggerFailed`.
- [ ] Run trigger tests and verify failure.
- [ ] Implement DOM, popup, worker, and injected native tiers behind small
  protocols; use human-like mouse movement for DOM controls.
- [ ] Re-run trigger tests and confirm green.
- [ ] Commit with `feat: trigger Jobright autofill with field attribution`.

### Task 5: Detection, safety, routing, and rate support

**Files:**
- Create: `app/agent/ats_detector.py`, `app/agent/humanize.py`,
  `app/agent/rate_limiter.py`, `app/agent/model_router.py`,
  `app/agent/gap_filler.py`
- Test: `tests/agent/test_ats_detector.py`, `test_humanize.py`,
  `test_rate_limiter.py`, `test_model_router.py`, `test_gap_filler.py`

**Interfaces:**
- `detect_ats(url, html) -> AtsKind`
- `Humanizer.sleep()/move_and_click()`
- `RateLimiter.check_and_record(board, action)`
- `ModelRouter.complete(question, complexity) -> ModelAnswer`
- `GapFiller.plan(fields) -> GapFillPlan`

- [ ] Write parameterized detector and deterministic humanizer tests; run red,
  implement, and run green.
- [ ] Write rate tests proving persisted daily cap and next-UTC-day reset; run
  red, implement against `Database`, and run green.
- [ ] Write router tests proving deterministic model selection and decimal cost
  accounting; run red, implement OpenAI-compatible HTTP calls behind an
  injected transport, and run green.
- [ ] Write gap tests proving canonical-answer precedence and protected-category
  denial even when a model offers a value; run red, implement YAML lookup and
  safety classification, and run green.
- [ ] Commit with `feat: add safe gap filling support`.

### Task 6: Board adapters and selector maps

**Files:**
- Create: `app/boards/{base,linkedin,jobright,wellfound,handshake,registry}.py`
- Create: `app/boards/selectors/{linkedin,jobright,wellfound,handshake}.yaml`
- Create: `tests/boards/test_adapters.py`

**Interfaces:**
- `BoardAdapter.open_listing(page, url) -> ListingResult`
- `BoardAdapter.start_application(page) -> ApplyResult`
- `adapter_for(board: Board) -> BoardAdapter`

- [ ] Write adapter tests with minimal page doubles and recorded fixture snippets
  proving selector-map loading, direct Jobright autofill, and distinct skip
  reasons for unsupported in-app flows.
- [ ] Run tests and verify absent adapters fail.
- [ ] Implement the common protocol, YAML selector loader, focused adapters, and
  registry.
- [ ] Re-run adapter tests and confirm green.
- [ ] Commit with `feat: add configurable job board adapters`.

### Task 7: Durable graph and approvals

**Files:**
- Create: `app/agent/approval.py`, `app/agent/graph.py`
- Test: `tests/agent/test_approval.py`, `tests/agent/test_graph.py`

**Interfaces:**
- `CliApprovalGate`, `ApiApprovalGate`
- `ApplicationState` typed dictionary
- `build_graph(dependencies, checkpointer)`
- `run_application(queue_id)` / `resume_application(thread_id, decision)`

- [ ] Write approval tests proving approve/reject validation and persisted actor,
  note, and timestamp.
- [ ] Run red; implement gate models and storage calls; run green.
- [ ] Write a fake-dependency graph test proving node order, unknown-ATS skip,
  durable interrupt, reject completion, approve completion, field attribution,
  and cost logging.
- [ ] Run red; implement narrowly-scoped nodes and LangGraph conditional edges
  with SQLite checkpointing; run green.
- [ ] Commit with `feat: add durable approval application graph`.

### Task 8: API, worker, CLIs, and documentation

**Files:**
- Create: `app/main.py`, `scripts/run_batch.py`, `scripts/export_log.py`,
  `README.md`
- Test: `tests/test_api.py`, `tests/scripts/test_cli.py`

**Interfaces:**
- `create_app(settings, worker_factory) -> FastAPI`
- `POST /queue`, `GET /runs/{id}`,
  `POST /applications/{id}/approve`, `/reject`

- [ ] Write API tests proving queue validation, status lookup, approval/rejection,
  and that one lifespan worker owns browser startup/shutdown.
- [ ] Run red; implement request/response models, routes, lifespan worker, and
  error mapping; run green.
- [ ] Write CLI parser tests for HTTP/local batch and CSV/JSON export; run red,
  implement clients, and run green.
- [ ] Document setup, dedicated profile, extension checks, fixture tests,
  adapters, API/CLI approval, privacy, and `xvfb-run`.
- [ ] Run `pytest -q` and static compilation; fix only demonstrated failures.
- [ ] Commit with `feat: expose job application control plane`.

### Task 9: Browser integration and final verification

**Files:**
- Create: `tests/integration/test_stub_extension.py`
- Modify: `README.md`

**Interfaces:**
- Uses the fixture server, unpacked extension, `BrowserSession`,
  `FormScanner`, and `JobrightTrigger`.

- [ ] Write a headed integration test proving DOM tier selection, asynchronous
  settle, changed-field attribution, and two intentionally remaining gaps.
- [ ] Run it and verify it fails because integration wiring is incomplete (or
  skips only for a documented missing browser/display prerequisite).
- [ ] Add test wiring without adding production-only hooks.
- [ ] Run `pytest -q`, `python -m compileall -q app scripts tests`, and the
  integration test under the available display or `xvfb-run`.
- [ ] Inspect `git diff --check` and scan docs/code for placeholders and unsafe
  submission defaults.
- [ ] Commit with `test: verify offline autofill workflow`.

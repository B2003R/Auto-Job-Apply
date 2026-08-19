# Task 9 report: the browser components, wired

## Status

DONE. **This build now types decided answers into live controls and submits
approved applications.** The two components the previous three reports
listed as the standing blocker — no field writer, no submitter — are
implemented, wired into `build_dependencies`, and exercised against a real
Chromium. `AUTO_SUBMIT` is still `false` and no blocker gate changed.

## Commits

Fifteen commits on `cursor/job-apply-agent-5d83`, on top of `78f346c`.

| SHA | Message |
|-----|---------|
| `06d86d8` | `fix(cli): keep a remote refusal off stdout under --json` |
| `6aaf355` | `refactor(scanner): make one in-page identity and one key derivation shared` |
| `a756847` | `feat(errors): name the refusals a writer and a submitter can raise` |
| `862c4bf` | `refactor(scanner): separate walking a page from reading a control` |
| `2ced2d2` | `feat(graph): hand the writer the control and the submitter its authorization` |
| `53b700c` | `feat(browser): type into a resolved control, guard the page, submit once` |
| `1f25566` | `feat(wiring): give the graph the real writer, guard, and submitter` |
| `c498353` | `test(fixtures): answer a submit locally, so a submission can be observed` |
| `a95d2a1` | `fix(browser): stop waiting on a frame that will never answer the guard` |
| `afee854` | `test(integration): drive the stub extension in a real browser, end to end` |
| `5515efd` | `docs: say what approving now does, and what it still refuses` |
| `2915443` | `fix(browser): refuse a mismatched control before typing into it, not after` |
| `555e723` | `fix(graph): claim the final press before making it, so a crash cannot repeat it` |
| `9ae8749` | `test: pin the rules mutation testing found nothing was holding` |
| `8ce4510` | `fix(scanner): stop waiting on a frame that will never answer a scan` |

Head: **`8ce4510`**. **Not pushed, no PR**, per the task instructions.

The brief prescribes one commit message (`test: verify offline autofill
workflow`). It is not used, because the brief's scope was one integration
test and the task as given also closes the production wiring gap — fourteen
of these fifteen commits are not that test. The integration suite is
`afee854`.

## Files changed

| File | Action |
|------|--------|
| `app/agent/browser_actions.py` | Created — writer, guard, submitter, and the page scripts (1203 lines) |
| `app/agent/errors.py` | Modified — 10 typed refusals for writing and submitting |
| `app/agent/form_scanner.py` | Modified — split `PAGE_TRAVERSAL_JS` out of `FIELD_IDENTITY_JS`; public `stable_key`; a per-frame deadline |
| `app/agent/graph.py` | Modified — `FieldWriter` takes a `FormField`, `Submitter` takes a `SubmitAuthorization`, guard before every action, the press is claimed |
| `app/main.py` | Modified — production wiring; `UnwiredFieldWriter`, `UnwiredSubmitter`, `ComponentNotWired` deleted |
| `app/storage/db.py` | Modified — `submit_attempts` table, `try_claim_submit`, `get_submit_attempt` |
| `app/storage/models.py` | Modified — `SubmitAttempt` |
| `scripts/run_batch.py` | Modified — remote refusals to stderr under `--json` |
| `README.md` | Modified — what approving does, submit limits, the crash rule, the browser suite |
| `tests/agent/test_browser_actions.py` | Created — 208 tests |
| `tests/integration/test_stub_extension.py` | Created — 16 tests, real browser |
| `tests/integration/browser.py` | Created — prerequisite resolution and `Xvfb` |
| `tests/integration/conftest.py` | Created — the one place Chrome is launched |
| `tests/test_ats_fixture_submit.py` | Created — 14 tests for the fixtures' local submit |
| `tests/fixtures/ats/fake_submit.js` | Created — a submit answered with no server |
| `tests/fixtures/ats/*.html` | Modified — load that script |
| `tests/agent/test_graph.py` | Modified — +3 classes for the new contracts |
| `tests/agent/test_graph_recovery.py` | Modified — the killed-mid-press case |
| `tests/storage/test_db.py` | Modified — `TestClaimingTheFinalClick` |
| `tests/test_api.py` | Modified — `TestTheShippedWiring` rewritten for production |
| `tests/test_readme.py` | Modified — +5 documentation assertions |
| `tests/agent/support.py` | Modified — fakes follow the new protocols |
| `tests/scripts/test_cli.py` | Modified — `TestJsonStdoutPurity` |

## Verification

```bash
python3 -m pytest -q                                  # 1748 passed (1460 before, +288)
python3 -m pytest tests --ignore=tests/integration -q # 1732 passed, no browser
python3 -m pytest tests/integration -q                # 16 passed in 29.7s, real Chromium
env -u DISPLAY python3 -m pytest tests/integration -q # 16 passed — Xvfb started by the suite
CHROME_EXECUTABLE=/nonexistent python3 -m pytest tests/integration -q -rs
                                                      # 1 passed, 15 skipped, reason names the variable
python3 -m mypy app scripts --ignore-missing-imports  # Success: no issues found in 34 source files
python3 -m compileall -q app scripts tests            # OK
git diff --check 78f346c..HEAD                        # clean
python3 /tmp/mutate9.py                               # 33 mutations, 32 caught, 1 accepted survivor
```

**Browser result: 16 passed.** The suite ran three ways — under the
inherited `DISPLAY=:1`, under an `Xvfb` it started itself with `DISPLAY`
unset, and with a deliberately broken `CHROME_EXECUTABLE` to check the skip
path names the thing to fix rather than failing obscurely.

`mypy app scripts tests` reports 63 errors in 20 files, all the pre-existing
`Settings(_env_file=None)` pattern that every test module in this repository
uses. One is in a file added here, for consistency with its neighbours; the
command the README documents (`app scripts`) is clean.

## What was built

### `PlaywrightFieldWriter`

Types one decided answer into one control, using the scanned field's
metadata rather than its key alone — which meant changing the protocol:

```python
async def write(self, page: Any, field: FormField, value: str) -> bool
```

The graph builds a `{key: FormField}` map from the open gaps and looks each
plan item up by the key its answer is recorded against. An item with no
scanned control is **not** typed; it becomes an `unwritten_answer` blocking
reason.

Resolution is two passes over every same-origin frame whose URL *and*
position in the frame tree match the field's (two iframes with the same
`src` is a common ATS shape, and a field scanned in the first must not be
written into the second). The first pass counts matches and describes the
one it found; exactly one across all frames is required. The second writes.

Three things make the write trustworthy rather than merely plausible:

- **Provenance.** The writer shares the trigger's `FormScanner`, re-derives
  the stable key from the control it actually resolved, and refuses a
  mismatch. This is why `build_dependencies` passes `trigger.scanner`: a
  scanner holds a random per-instance key, so a writer with its own would
  refuse every write. The check happens on the *counting* pass, before the
  value is sent (see self-review).
- **Events a framework notices.** `el.value = x` is invisible to a
  React-controlled input, so the native prototype setter is used and
  `input` and `change` are dispatched with `bubbles: true`.
- **Read-back in the page.** The control's value is compared with the
  intended one *in the page*, and only a boolean comes back. The value
  crosses the wire once, going in, and never appears in a refusal message
  or a log line.

Files, passwords, checkboxes, radios, multi-selects, and contenteditable
are refused before the page is touched at all — independently of the gap
filler, which already routes them to a human.

### `PlaywrightPageGuard`

Runs in every same-origin frame and raises `CaptchaEncountered` or
`LoginWallEncountered`. Two deliberate narrowings:

- **Structure, not prose.** Visible reCAPTCHA, hCaptcha, Turnstile, and
  Arkose containers, and a visible enabled password input. "Please verify"
  and a "Sign in" header link are *not* matched, because they appear on an
  enormous number of perfectly fillable pages and a false positive is a
  silent lost application. This is enforced structurally: `GUARD_SCRIPT` is
  built from `PAGE_TRAVERSAL_JS` only, so it has no `textContent` helper to
  read prose with even if somebody wanted to.
- **Visibility required for challenges.** An invisible reCAPTCHA v3 badge
  sits on pages that never challenge anyone. The response textarea is
  always hidden by design and is matched regardless.

A captcha outranks a login wall when both are present: it is the more
specific thing to tell an operator, and the one that stops a human too.

The guard runs before *every* browser action, not only at the two places it
did before: `open_listing`, `start_application`, `trigger_autofill`,
`fill_gaps`, and `submit`.

### `PlaywrightSubmitter`

Presses the last button, and reports success only when the page says so.

- **Authorization is a precondition it checks itself.** `SubmitAuthorization`
  carries the application id, the decision, and the blocking reasons;
  `approved` is true for an explicit approval, or for the `auto_submit` gate
  with no blocking reasons. The graph is structurally the only caller, but a
  new edge, a retry path, or a debugging script cannot submit something
  nobody released.
- **Exactly one visible final-submit control**, across the page and its
  same-origin frames, whose accessible name is one of 13 exact phrases
  after folding. Zero is `FinalSubmitControlNotFound` with every rejected
  name and reason attached; two is `FinalSubmitControlAmbiguous`. Neither
  clicks anything.
- **`Apply` and `Apply now` are not accepted.** On every board this project
  drives, those also *start* an application. Documented in the README as a
  limitation rather than solved by guessing.
- **`Next`, `Continue`, `Save`, `Review`, `Back`, `Upload` and their kin are
  never clicked**, so nothing advances a wizard on somebody's behalf.
- **One click.** A humanized pointer path, then polling for a concrete
  signal: navigation, a confirmation in a status/alert/heading region, or
  the marked form gone. No signal within the timeout is
  `submitted=False` with a screenshot and a reason saying so — never a
  second press.

### One approval, one press, even across a crash

Found in self-review and described below; it is the most consequential
change in this task after the components themselves. The press is claimed
in a new `submit_attempts` table before it is made, and the claim never
expires.

## TDD

Eight red-green cycles.

**1. The CLI.** `TestJsonStdoutPurity` first, red on prose in stdout, green
after routing both remote refusals through `_aside`.

**2. The scanner refactor.** Tests for `stable_key` reproducing a snapshot's
key, and for both page scripts sharing one identity helper. Red on the
missing method, green after extracting it. Then a second cycle: the guard
test `test_it_never_reads_the_pages_prose` was red because the shared
helper carried `textContent`, which is what produced the
`PAGE_TRAVERSAL_JS` / `FIELD_IDENTITY_JS` split.

**3. The browser components.** `tests/agent/test_browser_actions.py` written
against nothing — `ModuleNotFoundError: app.agent.browser_actions` — then
the typed errors, the protocol changes, and the module.

**4. The graph contracts.** `TestWhatTheWriterIsGiven`,
`TestWhatAuthorizesASubmission`, `TestWhenThePageIsChecked` before the node
changes.

**5. The wiring.** `TestTheShippedWiring` rewritten to assert production
components, red on `UnwiredFieldWriter`, green after
`build_dependencies`.

**6. The fixtures' local submit.** `tests/test_ats_fixture_submit.py` first,
red on the missing script.

**7. The documentation.** The `test_readme.py` assertions before the prose,
including one that the three success signals are each named.

**8. The single press.** A storage cycle (`TestClaimingTheFinalClick`) and a
graph cycle (`test_a_click_a_crash_interrupted_is_not_sent_a_second_time`),
both red first — the graph one demonstrating a genuine second click.

Four failures during these cycles were test bugs and are recorded rather
than quietly fixed:

1. `FakeFrame.evaluate` returned the handler's coroutine without awaiting
   it, so a fake that sleeps proved nothing. It awaits awaitable answers now.
2. The integration test asserted `unanswered_free_text == [textarea]`, but
   every empty text control is an unanswered free-text gap by design (that
   is what routes an optional phone number to the applicant). The assertion
   filters on `tag == "textarea"`, which is what the contract is about.
3. The integration test injected an `about:blank` iframe as a captcha
   marker. That hung — and the hang was a real defect in the guard, not in
   the test; see below. The marker is now a `div.g-recaptcha`, which is what
   a site operator actually writes.
4. `ApplicationField` keeps scanned attributes in `metadata`, not as
   columns; a test read `row.name`.

## Mutation testing

Thirty-three mutations, each applied by script to a pristine copy of the
real source, the focused suites re-run, the file restored. `git status`
clean afterwards. **Thirty-two are caught**; five were mis-aimed on the
first run (their anchors did not match, so they mutated nothing) and were
re-aimed, and two survivors were genuine test weaknesses now fixed.

| Mutation | Result |
|---|---|
| The submitter acts without an approval behind it | 1 failed |
| An ambiguous page is resolved by clicking the first candidate | 1 failed |
| Any accessible name is accepted as a final submit | 1 failed |
| `apply now` is treated as a final submit | 1 failed |
| An unconfirmed click is reported as a submission | 1 failed |
| A denied word is quietly dropped from the denylist | 1 failed |
| The denylist and the allowlist are allowed to disagree | 1 failed |
| The never-submit denylist is ignored entirely | **0 — accepted, see below** |
| The writer accepts more than one matching control | 1 failed |
| Human-only controls are typed into after all | 1 failed |
| The provenance key is not compared | 1 failed |
| The write is not verified against the control | 1 failed |
| A refused write is reported as a success | 1 failed |
| The writer sends the value to every candidate frame | **0** → 1 after re-aiming |
| A captcha loses to a login wall | 1 failed |
| The guard reads the page's prose | 1 failed |
| One unresponsive frame is allowed to hold the guard | **0** → 1 after fixing the test |
| The guard is not consulted before the final click | 1 failed |
| The guard is not consulted before autofill is triggered | 1 failed |
| The guard is not consulted before an answer is typed | 1 failed |
| The press is not claimed before it is made | 1 failed |
| A second press is made when the claim was already held | 1 failed |
| The press is claimed before the page is guarded | 1 failed |
| An answer with no scanned control is typed anyway | 1 failed |
| The writer is looked up by something other than the answer's key | **0** → 1 after adding a test |
| The writer is given a scanner of its own | 1 failed |
| No guard is wired into production | 1 failed |
| The submitter is wired without a screenshotter | 1 failed |
| Auto-submit is on by default | 1 failed |
| The stable key ignores the label it was scanned with | 1 failed |
| The stable key ignores which frame the control was in | 1 failed |
| A remote refusal goes back to stdout under `--json` | 1 failed |
| The fixture's fake submit lets the form navigate | 1 failed |

Three of these are worth recording.

**A guard with no timeout looked fine.** The mutation replaced the
per-frame deadline with `None` and the suite still passed — because the fake
unresponsive frame slept thirty seconds and *then* raised, which the guard
catches and skips. The test proved the guard eventually returns, which it
would have done anyway. The fake now never returns at all
(`asyncio.Event().wait()`) and the test bounds itself with
`asyncio.wait_for`, so an unbounded guard fails instead of passing slowly.

**The writer could have been handed the wrong control.** Replacing
`fields.get(item.key)` with "the first scanned control" survived, because
every test in that area had one gap on the form — where the two are the
same object. With two gaps they are not, and typing one person's cover
letter into the box the other answer belonged in is exactly the failure the
`FormField` change exists to prevent. `test_each_answer_is_paired_with_its_own_control`
now covers it.

**The accepted survivor: the Python denylist does nothing today.** Deleting
the `NEVER_SUBMIT_NAME` check changes no outcome, and that is provable
rather than accidental: the allowlist is *exact phrases*, so "Next" is
refused by not being on it, and no accepted phrase contains a denied word
(pinned by `test_no_accepted_phrase_is_itself_denied`). The denylist is a
guard on future edits — it becomes load-bearing the moment somebody adds a
broader phrase like "submit and continue". Rather than write a tautological
test, the list's contents are now pinned directly
(`test_the_denylist_still_names_the_words_it_promises_to`, 15 words), so a
word cannot vanish from the README's promise silently. The denylist in the
page script is separately covered by the browser test that puts `Next` on
the only visible control.

## Self-review notes

Reading the diff back found four defects. All four are fixed, each with a
test that fails without the fix.

- **A frame that never answers held the guard, a tab, a lease, and the
  queue.** Found because an integration test hung for over a hundred
  seconds. An `about:blank` iframe that is still notionally navigating has
  no execution context, and `frame.evaluate` waits for one; Playwright's
  default timeout did not preempt it as expected. The guard now bounds each
  frame at three seconds and treats a timeout exactly like an unreadable
  frame, which it already tolerated.
- **The scanner had the same defect, in two places.** Found by asking where
  else this project awaits `frame.evaluate` in a loop over frames.
  `FormScanner.snapshot` would have stalled *staging* rather than
  submission, and `_read_mutations` — which runs once per frame on every
  poll of the settle wait — was worse: an unbounded probe stops that wait
  inside an await, so the settle timeout that exists to end it can never be
  reached. Both are bounded now, with the frame reported as unscanned (which
  is already a coverage gap, and therefore a blocking reason at the gate) or
  as contributing no mutation count. Fixed rather than only reported because
  it is the same one-line pattern as the guard's and the defect is real.
- **A mismatched control was typed into before the mismatch was noticed.**
  The provenance check ran on the write pass's report, which is *after* the
  value has landed. The refusal was honest and the run recorded the answer
  as untyped, but somebody's salary would have been sitting in the wrong box
  on a form left open for them to finish. The counting pass now returns the
  identity of the single match, and a mismatch means the write pass never
  runs. The two passes each resolve the control separately, so the check
  after the write is kept for the case where the page repainted between them.
- **One approval could become two submissions.** The graph's own docstring
  promises "one approval means at most one submission across as many workers
  as you care to start", enforced by a per-thread execution lease. That
  lease deliberately expires, so a killed worker's thread can be recovered —
  and a worker killed *after* the press leaves a thread indistinguishable
  from one whose press never happened: approval on file, no outcome
  recorded, interrupt still in the checkpoint. The existing suite already
  proved the submit node is replayed in that situation; with a real
  submitter wired in, that replay is a second application in somebody's
  name. The press is now claimed in `submit_attempts` before it is made, the
  claim does not expire, and a successor that finds it fails the application
  naming the earlier attempt instead of pressing again.

  The claim is taken *after* the page guard, so a captcha that appeared
  while the application sat at the gate does not spend the one attempt. The
  cost is real and is documented: a hard crash between the claim and the
  press — as opposed to after it — is now unrecoverable too, because from
  the outside the two are the same event. That trades a recoverable
  application for a duplicate that cannot be taken back, which is the
  direction this project trades in everywhere else. Two existing tests
  encoded the old behaviour and were rewritten to crash in the guard, which
  is genuinely before the press, and they still recover.

Two smaller things: `except (Exception, asyncio.TimeoutError)` was
redundant (`asyncio.TimeoutError` is `TimeoutError` on 3.11+, already an
`Exception`) and is now a plain `except Exception` with the reason in the
comment; and the integration module carried four imports left over from
before its fixtures moved into `conftest.py`.

## Design notes

### Why the protocol changed rather than the key

`GapFillItem` carries a key, a label, a name, a type, and two flags — not
the frame URL, form, control id, or shadow path a writer needs to find a
control. Three options: fabricate a `FormField` in the writer from the
item (a guess), look the field up in a side-channel the writer holds (state
the graph would have to keep in sync), or pass the field. The field is
passed, and the *key* the answer is recorded against comes along with it, so
the audit row and the control cannot drift apart.

### Why the submitter checks its own authorization

The graph only routes to `submit` after the gate resolves, so an
authorization argument is redundant *today*. It is there because the
submitter is the one component in this project whose mistakes cannot be
undone, and "the only caller is careful" is a property of the current graph
rather than of the submitter. `SubmitNotAuthorized` is asserted directly
against the production object in `tests/test_api.py`, with no browser.

### Why the fixtures answer their own submit

A browser test needs a submission to observe and must not make one. The
fixtures load `fake_submit.js`, which cancels the navigation, shows a
confirmation in a `role="status"` region, and removes the form — two of the
three signals the submitter accepts, produced with nothing behind them. It
deliberately does not bypass HTML5 constraint validation, so the browser
test only reaches the submit handler because the writer really filled the
required field.

### Why the integration suite starts its own Xvfb

The browser is always headed, project-wide. Wrapping `pytest` in `xvfb-run`
would affect the 1732 tests that need no display, and a machine without
`Xvfb` would produce a browser that fails to launch for reasons nobody can
read. The suite uses `$DISPLAY` when there is one, starts an `Xvfb` on a
free display number when there is not, and skips naming `xvfb` when it can
do neither. Every prerequisite skip names the executable, the environment
variable, or the package that would fix it.

### Why one place launches Chrome

The launch flags are the part of this suite easiest to get subtly wrong and
hardest to notice — `--disable-extensions-except`, the two default args
Playwright sets that would switch extensions off, and an `env` that must
*extend* `os.environ` rather than replace it (replacing it drops
`XAUTHORITY`, and the browser then cannot authenticate to a display it can
see; that cost an hour). They live in `tests/integration/conftest.py` so a
second browser test file cannot launch Chrome a second, different way.

## Concerns

Task 8's concerns 3, 4, and 5 stand. Its concerns 1 and 2 — no writer, no
submitter, no guard — are **closed**. The new list:

1. **A submission can still be unconfirmed, and that is not a bug.** Three
   signals cover the ATS pages I can test against. A page that confirms
   some fourth way records a **failed** application that may well have been
   submitted. The screenshot and the reason are what an operator has; the
   README says to check the ATS. Erring the other way — treating a click as
   a submission — would write false success into the audit log, which is the
   one thing this project is built not to do.
2. **A permanently pending frame now costs three seconds per poll rather
   than a hang.** With the deadlines in place, a page carrying such a frame
   makes the settle wait's polls three seconds apart instead of a hundred
   milliseconds, so it reaches its own timeout after a handful of polls
   rather than a hundred and fifty. Correct and bounded, but a page like
   that will settle-timeout where it might otherwise have settled. A frame
   that has missed its deadline once could be dropped from subsequent polls;
   that is a behaviour change to the settle logic and did not belong in this
   fix.
3. **A hard crash between the claim and the press is unrecoverable.**
   Described above. The application is recorded failed and a human has to
   look; the alternative is a duplicate. If this proves common in practice,
   the claim could carry a "pressed" flag written immediately after
   `move_and_click` returns, which would narrow the ambiguous window to the
   click itself rather than the whole attempt.
4. **The accessible-name allowlist is 13 phrases and will be too narrow.**
   Every ATS that words its final button differently reports "no
   final-submit control" and leaves the form filled. That is the safe
   direction, and it is documented, but the first thing an operator will
   want is to add a phrase — and there is no configuration for it, on
   purpose. A `FINAL_SUBMIT_PHRASES` extension point would need to be
   validated against the denylist at load time, which is why it is not a
   setting today.
5. **The guard cannot see a challenge in a cross-origin frame.** Which is
   where reCAPTCHA's widget actually lives. The container is matched
   instead, which is same-origin and is what a site operator writes — so a
   challenge whose container is injected by a third-party script into a
   cross-origin frame is invisible to it. The scanner reports the skipped
   frame as a coverage gap, which is itself a blocking reason at the gate,
   so the approval gate is the backstop.
6. **`select` writing matches an option by value or visible text, exactly.**
   A country dropdown offering "United States of America" against an answer
   of "United States" is a refusal, not a near match. Deliberate — fuzzy
   matching a select is choosing on somebody's behalf — but it is the
   likeliest source of `unwritten_answer` in practice.
7. **The integration suite runs in the default `pytest` invocation and takes
   thirty seconds.** It skips cleanly without a browser, so CI is not
   broken by it, but it is 30s on every full run and it launches a real
   browser on a developer's machine without being asked. A marker
   (`-m "not browser"`) would be the conventional answer; it was left in by
   default because a browser suite nobody runs is the state this task
   existed to end.
8. **`tests/integration` shares one persistent context per test.** Each
   test gets a fresh profile and a fresh context, which is correct and also
   why the file takes thirty seconds rather than five. A session-scoped
   context with per-test page resets would be faster and would couple the
   tests to each other's page state; the current shape is the safer one for
   a suite whose whole job is observing what a page does.
9. **Nothing verifies the *real* Jobright extension.** The suite proves the
   code operates a page with *an* unpacked MV3 extension whose sidebar sits
   in an open shadow root. A real build that renders differently, or whose
   Autofill control is worded differently, is still only checked by
   `doctor` on the operator's own machine.

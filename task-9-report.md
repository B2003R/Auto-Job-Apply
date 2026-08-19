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

---

# Task 9 addendum: the review's Critical and Important findings

## Status

DONE. Seven findings, all closed, each with tests that fail without the fix.
Six of the seven were **false successes or hangs in the submit path** — the
one place in this project where being wrong costs somebody an application
that was never sent, or one sent twice. `AUTO_SUBMIT` is still `false`, no
blocker gate changed, and nothing here submits anything external.

The one-line version of each: the submitter judged a child frame's press by
looking at the top page; a navigation on its own counted as a submission; a
pending `about:blank` frame could hang the node before or after the press;
the guard called the markup a reCAPTCHA v3 key leaves a challenge; the press
was a pointer event at a remembered coordinate; the application's one
durable attempt was spent by refusals that clicked nothing; and no test
drove a form in an iframe or a control in a shadow root in a real browser.

## Commits

Seven commits, on top of `95bf538` (the report above).

| SHA | Message |
|-----|---------|
| `b026fe2` | `fix(browser): stop waiting on a frame that never answers the submitter` |
| `4d5c256` | `fix(browser): tell a challenge apart from the markup a v3 site key leaves` |
| `f0bf6e4` | `fix(browser): judge a submission against each target's own pre-click state` |
| `7e2d278` | `fix(browser): let the driver verify and make the last click, not a coordinate` |
| `956a803` | `fix(graph): hand the submitter the one press, claimed just before the click` |
| `e201dfc` | `test(integration): drive the two places a form is not the top document` |
| `8640399` | `docs: say what confirms a submission now, and what no longer does` |

Head: **`8640399`**. **Not pushed, no PR**, per the task instructions.

## Files changed

| File | Action |
|------|--------|
| `app/agent/browser_actions.py` | Modified — `PageState`/`SubmitVerdict` and `submission_verdict`; `SUBMIT_STATE_SCRIPT` reports readings rather than verdicts; `ACTIVE_CAPTCHA_JS` shared by guard and submitter; per-frame deadlines on every question; trusted click with actionability verification; the permit claimed before the click |
| `app/agent/errors.py` | Modified — `FinalSubmitControlNotActionable`, `SubmitPermitAlreadyUsed`, `SubmitPermitNotClaimed` |
| `app/agent/graph.py` | Modified — `SubmitPermit`; `Submitter` takes one; the submit node builds it and verifies it was claimed |
| `app/agent/humanize.py` | Modified — `move_to` (travel without a press); `move_and_click` built on it |
| `README.md` | Modified — what confirms a submission and what no longer does; a refused press costs nothing; the guard's passive-markup exclusion; the two new fixtures |
| `tests/agent/test_browser_actions.py` | Modified — +5 classes (baselines, navigation, page refusals, verified click, when the press is claimed) |
| `tests/agent/test_graph.py` | Modified — `TestTheOnePressEachApplicationGets` |
| `tests/agent/test_graph_recovery.py` | Modified — crash-before-claim and crash-after-claim are now distinct cases |
| `tests/integration/test_stub_extension.py` | Modified — passive/active captcha in a browser, an overlaid control, and the iframe and shadow-root suites |
| `tests/fixtures/ats/iframe_host.html`, `iframe_form.html` | Created — a form in a same-origin child frame, under a standing confirmation-shaped banner |
| `tests/fixtures/ats/shadow_form.html`, `shadow_form.js` | Created — a field and a submit control in open shadow roots, answering their own press |
| `tests/fixture_server.py` | Modified — `NESTED_FIXTURES` |
| `tests/test_ats_fixture_submit.py` | Modified — offline checks that the new fixtures submit nowhere and bait the bug they exist for |
| `tests/test_api.py`, `tests/agent/support.py` | Modified — the permit through the shipped wiring and the fakes |
| `tests/test_readme.py` | Modified — the confirmation rules and the cost of a refusal are pinned |

## Verification

```bash
python3 -m pytest -q                                    # 1835 passed (1748 before, +87)
python3 -m pytest tests --ignore=tests/integration -q   # 1810 passed, no browser
python3 -m pytest tests/integration -q                  # 25 passed in 50s, real Chromium
env -u DISPLAY python3 -m pytest tests/integration -q   # 25 passed — Xvfb started by the suite
CHROME_EXECUTABLE=/nonexistent python3 -m pytest tests/integration -q -rs
                                                        # 1 passed, 24 skipped, reason names the variable
python3 -m mypy app                                     # Success: no issues found in 29 source files
python3 -m compileall -q app tests scripts              # OK
git diff --check 95bf538..HEAD                          # clean
python3 /tmp/mutate10.py                                # 12 mutations, 11 caught offline, 1 caught by the browser suite
```

**Browser result: 25 passed** (16 before, +9), three ways — under the
inherited `DISPLAY=:1`, under an `Xvfb` the suite started itself with
`DISPLAY` unset, and with a deliberately broken `CHROME_EXECUTABLE` to check
that the skip still names the thing to fix.

`mypy app tests` still reports the pre-existing `Settings(_env_file=None)`
errors that every test module in this repository has; nothing added here
contributes a new one, and `mypy app` is clean.

## Finding by finding

### 1 & 2. Confirmation: per-target baselines, and navigation is not enough

These are one change, because they are one function. `SUBMIT_SIGNAL_SCRIPT`
used to decide in the page whether a submission had happened and hand back a
verdict; it is now `SUBMIT_STATE_SCRIPT`, which reports one target's
*readings* — `location.href`, the visible text of every confirmation-shaped
region, every reason to think the page refused, and whether the marked form
is still in this document — as a frozen `PageState`. Python compares two of
them.

**Per-target baselines.** Every target that will be polled is read *before*
the click, and only ever compared with its own reading. The targets are the
submitting frame and the top document, deduplicated by identity so a form in
the main frame is one target rather than two. The submitting frame's baseline
is mandatory: a press whose outcome cannot be judged is not one to make, so
a frame that cannot be read before the click is a refusal that clicks
nothing.

The bug this closes: for a form in a same-origin iframe, the frame's
pre-click URL and form presence were compared against the *top page's*
readings. Those differ by definition — the top document is at a different
URL and never contained the marked form — so the first poll of every such
submission reported both a navigation and a vanished form. It also means a
"thank you for applying" panel the page was already showing can no longer
succeed, because the comparison is against text that was already there.

**Navigation is not enough.** `submission_verdict` accepts exactly two
things: a confirmation that is *not* in the baseline, or a
`SUCCESS_DESTINATION` together with the marked form being gone. It refuses
outright on a `REFUSED_DESTINATION` (sign-in, auth, captcha, challenge,
error, expired) and on any *fresh* blocker — a validation message, a
password field that was not there, an active challenge — which ends the wait
immediately rather than spending the whole timeout on a page that has
already said no. A refusal outranks a confirmation on the same reading,
because a sign-in page is not made trustworthy by the words on it.

Ordering inside the function is deliberate and tested: fresh blockers, then
a refused destination, then a new confirmation, then a success destination
with the form gone.

### 3. Every question the submitter asks a frame is bounded

An `about:blank` child that is still notionally navigating has no execution
context, and `frame.evaluate` waits for one indefinitely. Every submitter
question now goes through one helper (`_ask`) with a per-frame deadline
(three seconds by default), and during the confirmation poll that allowance
is additionally narrowed to whatever is left of the overall deadline — so
the sum of the per-frame waits cannot outlive the confirmation timeout. The
`evaluate_handle` that resolves the control and the `bounding_box` call are
bounded the same way.

A frame that does not answer is treated as what it is in each position:
skipped while counting candidates (the scanner already reports it as a
coverage gap, which is a blocking reason at the gate), a refusal for the
pre-click baseline, and silence — never a signal — while polling.

### 4. A challenge, not the markup a v3 key leaves everywhere

reCAPTCHA v3 scores visitors on an enormous number of ordinary pages and
challenges almost none of them, leaving a corner badge and an `api2/anchor`
iframe behind either way. Matching those (or a bare `data-sitekey`) abandoned
perfectly fillable applications as `captcha_required` — a loss nobody can
tell was wrong, because the operator sees the reason a real challenge
produces.

Active evidence is now required: the challenge frame (`api2/bframe`,
hCaptcha's challenge frame, Turnstile, Arkose), an enforcement interstitial,
a captcha inside an open `dialog`/`[role="dialog"]`, or a widget the page has
actually rendered at a size a person could use. Markup that declares itself
invisible is excluded even where the layout reserved a box for it. The rule
lives in one `ACTIVE_CAPTCHA_JS` constant shared by the guard and the
submitter, because the submitter needs the same answer after its click: a
challenge that appears then is a submission that did not happen.

Both directions are tested in a real browser: a v3 badge with its anchor
frame and an invisible-sized widget in a reserved box passes the guard, and
an `api2/bframe` served from the loopback fixture server does not.

### 5. The press is the driver's own click

The press was `mouse.move`/`down`/`up` at the bounding box's remembered
centre. Between resolving a control and pressing it, a cookie banner can
animate in over it, a sticky footer can cover it, or the node can detach —
and a coordinate press lands on whatever is actually there, after which the
run waits out its confirmation timeout for a signal that whatever was
clicked was never going to produce.

Now: `scroll_into_view_if_needed`, a bounding box (still rejected if it is
page-sized, because hit-target verification is perfectly happy to click a
full-viewport wrapper), then `click(trial=True)` — the driver's actionability
and hit-target checks with the press withheld — then the humanized pointer
travel, then `click()`. A failed check is
`FinalSubmitControlNotActionable` with nothing clicked; a failure *after* the
press carries `pressed=True` and is never followed by a second attempt.

`Humanizer.move_to` is the travel without the press;
`Humanizer.move_and_click` still exists for the extension sidebar button,
which is not a form control and has no element handle, and is now built on
`move_to`.

In a browser: a full-viewport transparent overlay over the submit button
produces `FinalSubmitControlNotActionable` rather than an eight-second wait
ending in an unconfirmed application somebody has to check by hand.

### 6. The one press, claimed immediately before it is made

The durable claim never expires, so spending it spends the application's only
attempt. It was taken as the submit node started, which meant every refusal
that followed spent it: a control the accessible-name rules reject, two
controls, a banner over the only one. Those click nothing, so the form was
still perfectly submittable — and the operator who fixed the page found an
application that could never be sent.

`SubmitPermit` is a typed one-shot: `claim()` runs the durable claim exactly
once, `claimed` is true only if the record was really written, and a claim
the database *refuses* still spends the permit (a second try would race the
first). The graph builds one per submit node and hands it to the submitter;
the submitter resolves the control, checks the name rules, satisfies the
driver that it can be clicked, and then claims, with nothing between the
claim and `element.click()`. Afterwards the graph checks the permit really
was claimed: returning a `SubmitOutcome` is a report that the control was
pressed, and an unclaimed permit alongside one is `SubmitPermitNotClaimed`
and a failure, because a press with no durable record leaves the next replay
free to press again.

The two recovery cases are now distinct tests rather than one shared helper:
a crash *before* the claim leaves the application submittable and a
successor presses it, and a crash *after* the claim leaves the successor's
submitter refused at the claim with nothing pressed.

This also narrows Task 9's concern 3 (a crash between the claim and the
press being unrecoverable) as far as it can be narrowed without a second
durable write: the ambiguous window is now the click itself rather than the
whole submit node.

### 7. The two places a form is not the top document

Both false-success paths above live in shapes the offline Greenhouse fixture
does not have, so both are fixtures now, driven by the real `FormScanner`,
`PlaywrightFieldWriter`, and `PlaywrightSubmitter` in a real Chromium on
loopback.

- **`iframe_host.html`** holds the form in a same-origin child frame
  (`iframe_form.html`) and shows, before anything is clicked, a
  `role="status"` banner reading "Thank you for applying to two other roles
  this month" — which is what a "you have applied to N roles" panel looks
  like to a machine. Three tests: the writer crosses the frame boundary and
  the key survives it; a press in the frame is confirmed by that frame's own
  before-and-after; and when the frame swallows its own submit, the top
  page's standing banner is **not** this application's confirmation.
- **`shadow_form.html`** puts the last required field and the final submit
  control in open shadow roots and answers its own press in
  `shadow_form.js`, because neither native submission nor native constraint
  validation crosses a shadow boundary. It refuses a press whose field was
  never filled, which is what stops the submitter tests passing on a form
  the writer got wrong. Three tests: the shadow path is built the same way
  by the scanner and the writer, the control in the shadow root is what gets
  pressed and confirmed, and a page that says it refused the press is not a
  submission.

Offline checks in `tests/test_ats_fixture_submit.py` guard the fixtures
themselves: no `action=`, no `fetch`, no `XMLHttpRequest`, the two
confirmation wordings are asserted *equal* (two scripts is one more than one)
and both are run through the shipped `is_confirmation_text`, and the
iframe host's banner is asserted to be text the shipped predicate really
does recognise — a fixture whose bait the code ignores would pass whether or
not the bug was fixed.

## TDD

Six red-green cycles, in the order the code depends on:

1. **Bounded questions.** `TestNoFrameCanHoldTheSubmitter` first, with a fake
   frame that never returns at all, each test bounded by `asyncio.wait_for`
   so an unbounded submitter fails rather than hanging the suite.
2. **The captcha rule.** `TestWhichCaptchaMarkupIsActuallyAChallenge` and two
   browser tests (one false positive, one false negative) before the
   selector split.
3. **The confirmation rework.** `TestEachTargetIsJudgedAgainstItsOwnBaseline`,
   `TestNavigationAloneNeverConfirms`, and
   `TestThePageSayingItRefusedTheSubmission` — red on `ImportError` for
   `PageState`/`submission_verdict`, which did not exist yet.
4. **The verified click.** `TestThePressIsAVerifiedClickRatherThanACoordinate`
   before `trial=True` existed, plus the overlay test in the browser.
5. **The permit.** `TestTheOnePressEachApplicationGets` in the graph tests and
   `TestWhenTheOnePressIsClaimed` in the browser-action tests, red on
   `SubmitPermitAlreadyUsed` not existing.
6. **The nested fixtures.** The offline fixture checks first, then the browser
   suites.

One test-shape decision worth recording. The double-based confirmation tests
initially assumed "the first state reading is the baseline", which broke
because `FakePage.evaluate` delegates to `main_frame`: a single-frame page was
asked twice per poll. The right fix was not a smarter double but a smarter
target list — `_confirmation_targets` compares the submitting frame with the
page's *main frame* by identity, so one document is one target. The double
now models real Playwright, and the code no longer asks the same document
twice.

## Mutation testing

Twelve mutations, each applied by script to the real source, the focused
suites re-run, the file restored; `git status` clean afterwards. Four were
mis-aimed on the first pass (their anchors did not match, so they mutated
nothing) and were re-aimed. **All twelve are caught** — eleven by the offline
suites, one only by the browser suite, which is recorded below.

| Mutation | Result |
|---|---|
| A bare navigation confirms a submission again | 1 failed |
| A confirmation the page was already showing counts | 1 failed |
| Every target is judged against the submitting frame's state | 1 failed |
| A fresh rejection on the page is not a refusal | 1 failed |
| A refused destination is accepted after all | 1 failed |
| The submitter's questions are unbounded again | 1 failed |
| The passive markup a v3 key leaves is an active challenge | offline: **0** — browser: 1 failed |
| The press is a coordinate again, unverified | 1 failed |
| The press is claimed before the control is checked | 1 failed |
| The graph does not check the permit was claimed | 1 failed |
| The graph claims the press before the submitter runs | 22 failed |
| A permit can be claimed twice | 1 failed |

Four are worth recording.

**The passive-captcha exclusion is only provable in a browser.** The offline
tests can assert which selectors are in which list and that both the guard
and the submitter splice the same shared script, but "this widget is
invisible" is a computed-style question only a real DOM answers. Removing the
exclusion therefore survives the offline suite and is caught by
`test_the_guard_ignores_the_markup_recaptcha_v3_leaves_everywhere`. That test
was itself strengthened for exactly this reason: the invisible widget is
given a *reserved box* (`304×78`), so the size check would call it active if
the passive check were not doing the work.

**A shared baseline is caught in the browser too, and by name.** With
`_baselines` handing every target the submitting frame's reading, the iframe
test fails with `submitted=True, reason="the page showed a confirmation it
was not showing before the click: 'Thank you for applying to two other roles
this month.'"` — the reported false success, in the exact words the fixed
code would use for a real one.

**Claiming the press before the submitter runs breaks twenty-two tests.**
That is the old behaviour, and the size of the blast radius is the point: it
is not one test guarding an edge case but the recovery suite, the failure
paths, and the permit contract all disagreeing at once.

**Deep traversal and cross-frame writing are load-bearing in the new
fixtures.** Two extra probes confirmed the fixtures exercise what they are
for rather than passing incidentally: restricting the submit-candidate search
to `document` makes both shadow submitter tests fail with
`FinalSubmitControlNotFound`, and restricting the writer to the main frame
makes all three iframe tests fail with `FieldNotUniquelyResolved`. Perturbing
the shadow path the writer sends fails the shadow parity test, which is the
scanner/writer disagreement that test exists to catch.

## Self-review notes

- **`FakeSubmitter`'s unapproved branch contradicted the new protocol.** It
  *returned* an outcome describing a refusal, where the real submitter
  raises `SubmitNotAuthorized`. Under the permit contract a returned outcome
  means "I pressed it", so the fake would have been reporting a press it
  never claimed. It raises now, like the production object.
- **The pointer travel happens before the claim, deliberately.** Moving a
  pointer submits nothing, and the requirement is that the claim comes after
  every check that could still refuse. Putting the claim before the travel
  would spend the attempt on a page whose humanizer then threw.
- **The README's own test pinned the wrong promise.** It asserted "the three
  success signals are documented", which is exactly the sentence that made a
  sign-in redirect a submitted application. Replaced with assertions that
  the prose says a navigation on its own is *not* one, and that a refused
  press costs nothing.

## Concerns

Task 9's concerns 1, 4, 5, 6, 7, 8, and 9 stand. Concern 2 (a pending frame
costing three seconds per settle poll) stands and now applies to the
submitter's confirmation poll as well, bounded by the same reasoning.
Concern 3 is narrowed as described under finding 6. New:

1. **A confirmation this build does not recognise is still a failed row.**
   The accepted signals are narrower than before, on purpose: an ATS that
   confirms only by, say, swapping a heading that does not match
   `CONFIRMATION_TEXT`, without navigating anywhere, now records a **failed**
   application that may well have been submitted. That is the direction to
   err, and the screenshot and reason say so, but the narrowing does convert
   some previously-"submitted" rows into rows a human has to check.
2. **`SUCCESS_DESTINATION` and `REFUSED_DESTINATION` are word lists over a
   URL.** `/verify` is in the refused list because an expired session lands
   there, but a genuine "verify your email to finish" step after a
   submission would be refused by it. Neither list is configurable, for the
   same reason `FINAL_SUBMIT_PHRASES` is not.
3. **Fresh-blocker detection can end a wait early on a page that was going
   to confirm.** A form that shows "Please correct the highlighted field"
   *and then* accepts the submission anyway would be recorded as refused.
   The veto requires the marker to be new since the click and the text to
   read like a rejection, which is as narrow as it can be made without
   waiting out the clock on pages that have already said no.
4. **The shadow fixture answers its own press in JavaScript.** It has to —
   neither native submission nor native validation crosses a shadow
   boundary — but that means its "the form validated" behaviour is the
   fixture's own code rather than the browser's, unlike the four ATS
   fixtures, which rely on real constraint validation.
5. **The permit is one shot per submit-node invocation, not per
   application.** The durable claim in `submit_attempts` is what makes it
   per-application; the permit is the in-process handle to it. A future
   caller that built two permits for one application would get the second
   one's claim refused by the database, which is the right outcome, but the
   type itself cannot express "there is only one of me".
6. **`tests/integration` is now 50 seconds and nine tests longer.** Each
   test still gets a fresh persistent context and profile, which is the safe
   shape for a suite that observes what a page does, and the browser fixtures
   for the two new pages open their own tab in that context rather than
   sharing the Greenhouse one.

---

# Task 9 addendum 2: the remaining Critical, and four hardenings

## Status

DONE. The remaining Critical is closed, along with four smaller things next
to it. `AUTO_SUBMIT` is still `false`, no blocker gate changed, and nothing
here submits anything external.

The Critical, in one line: confirmation *freshness* was a comparison of
text, so a careers page whose standing thank-you panel counts, cycles, or
rebuilds itself produced a confirmation nobody had been showing on the first
poll after any click at all — an application recorded as sent, an approval
spent, and a queue row completed for a form that never went anywhere. The
four hardenings: `?submitted=false` counted as a success destination; the
field writer's evaluations and the submitter's scroll were the last
unbounded waits left; every application's screenshots overwrote the last
one's; and every browser run printed a Playwright teardown trace that read
like a crash.

## Commits

Five commits, on top of `64693a2` (the addendum above), plus this report.

| SHA | Message |
|-----|---------|
| `2c465c6` | `fix(submit): judge confirmation freshness by region, not by wording` |
| `72e7354` | `fix(submit): a success destination has to say so affirmatively` |
| `ade94f9` | `fix(writer,submit): bound the two evaluations that were still unbounded` |
| `4323776` | `fix(artifacts): one screenshot per application, per attempt` |
| `cdcaf6a` | `test(integration): stop asking Playwright a question that prints a crash` |

**Not pushed, no PR**, per the task instructions.

## Files changed

| File | Action |
|------|--------|
| `app/agent/browser_actions.py` | Modified — `ConfirmationRegion` and `_fresh_confirmations`; `SUBMIT_STATE_SCRIPT` stamps and reports a region identity; `SUCCESS_PATH`/`NEGATED_SUCCESS`/`SUCCESS_QUERY_KEY` with a parsed query; the writer's per-frame deadline; a bounded scroll; screenshots named after the application |
| `app/main.py` | Modified — `ArtifactScreenshotter` never overwrites, and a name never decides where a file goes |
| `README.md` | Modified — what makes a confirmation a new one, what a success destination has to say, and the fixture that will not hold still |
| `tests/agent/test_browser_actions.py` | Modified — `TestWhatMakesAConfirmationANewOne`, `TestNoFrameCanHoldTheWriter`, truthy/negated destinations, the screenshot's name, a control that never finishes scrolling |
| `tests/test_api.py` | Modified — `TestTheScreenshotsAnOperatorHasToLookAt` |
| `tests/integration/test_stub_extension.py` | Modified — `TestAConfirmationShapedPanelThatWillNotHoldStill` (five real-Chromium regressions) |
| `tests/fixtures/ats/live_status.html`, `live_status.js` | Created — a standing thank-you panel that ticks, rebuilds itself, has no id, or counts the press, plus the neutral status region the happy case confirms into |
| `tests/fixture_server.py` | Modified — `LIVE_REGION_FIXTURES` |
| `tests/test_ats_fixture_submit.py` | Modified — offline checks that the panel is bait and that the region it confirms into starts neutral |
| `tests/integration/browser.py` | Modified — the Chromium path is asked for in a subprocess |
| `tests/test_readme.py` | Modified — the freshness rule is pinned in the prose |

## Verification

```bash
python3 -m pytest -q                                    # 1872 passed (1835 before, +37)
python3 -m pytest -q --ignore=tests/integration         # 1842 passed, no browser
DISPLAY=:1 python3 -m pytest tests/integration -q       # 30 passed in 65s, real Chromium
env -u DISPLAY python3 -m pytest tests/integration -q   # 30 passed in 67s — Xvfb started by the suite
CHROME_EXECUTABLE=/nonexistent python3 -m pytest tests/integration -q -rs
                                                        # 1 passed, 29 skipped, reason names the variable
python3 -m mypy app                                     # Success: no issues found in 29 source files
python3 -m compileall -q app tests scripts              # OK
git diff --check 64693a2..HEAD                          # clean
```

**Browser result: 30 passed** (25 before, +5), both under the inherited
`DISPLAY=:1` and under an `Xvfb` the suite starts itself — and now without
the Playwright teardown trace that used to follow every run.

`mypy app tests` still reports the pre-existing `Settings(_env_file=None)`
errors present in every test module here; nothing added contributes a new
one, and `mypy app` is clean.

## Finding by finding

### 1. Freshness is a property of the region, not of its wording (Critical)

`PageState.confirmations` was a tuple of *texts*, and a confirmation counted
as new when its text was not in the pre-click reading. Every careers page
with a standing "you have applied to N roles this month" panel therefore
confirmed every click: the panel's wording changes on a timer, so the very
first poll found confirmation-shaped text nobody had been showing.

It is now a tuple of `ConfirmationRegion(identity, text)`. The state script
addresses each confirmation-shaped region in three fallbacks, in the order
they survive a repaint: an identity it has already stamped on the node
(`data-jobright-confirmation-region`), the page's own `id` for it, and
failing both a token minted and stamped now. The shadow-root path prefixes
all three, so an `id` inside a component cannot collide with the same `id` in
the light DOM.

`_fresh_confirmations` then requires **both** halves, and each is
load-bearing in a different direction:

* a region whose *identity* is in the baseline is not new, however its text
  changed — the counting panel;
* a region whose *text* the target was already showing is not new, however
  new the node is — the framework that rebuilds its banner rather than
  editing it, which an identity rule alone would read as a confirmation
  arriving.

What must not break, and does not: the empty `role="status"` region almost
every ATS confirms into is not in the confirmation *baseline*, because
nothing that is not already shaped like a confirmation ever is. The moment
it reads like one it is a new region by both halves of the rule.

A region reported without both an identity and a text is dropped rather than
guessed at, which costs an honest "not confirmed".

**Real Chromium.** `live_status.html` is the page this could not be proved
without: a panel that rewrites its own text on a 60ms timer, one that throws
its node away and rebuilds it (same `id`, new element), one with no `id` at
all, and one that bumps its count the instant the button is pressed. All four
swallow the submit, so the honest answer to each is "not confirmed". The
fifth test is the ordinary case on the same restless page — a neutral
`role="status"` region that the click fills in — and it still confirms, with
the panel counting throughout and the reason naming the banner rather than
the counter.

### 2. A success destination has to say so affirmatively

`SUCCESS_DESTINATION` was one word list searched over the path and the query
joined together, so `?submitted=false` — how an ATS records a draft —
matched, as did `?submitted=` and `/application/not-submitted`. On the one
acceptance path where a vanished form is already half the evidence, a
validation round trip that reloaded with a state flag was a submitted
application.

The path is now matched on its own (`SUCCESS_PATH`), a negation anywhere in
it disqualifies the URL outright (`NEGATED_SUCCESS`), and the query is
*parsed*: a key that could carry a claim (`submitted`, `confirmed`,
`success`, `complete`, camel-cased or separated) only carries one when its
value is on `TRUTHY_QUERY_VALUES`. `?submitted=true`, `?success=yes`,
`?applicationSubmitted=TRUE` count; `?submitted=false`, `?submitted=0`,
`?submitted=`, `?not_submitted=1` do not.

### 3. The last two unbounded waits

The guard, the scanner, and the submitter's questions all stop waiting on a
frame with no execution context. Two calls did not: every `evaluate` the
field writer makes, and the submitter scrolling its control into view. A
worker stuck in the writer holds a half-filled form, a lease nobody renews,
and the queue behind it.

* A frame that stops answering while its controls are counted is a
  **refusal** naming the silent frame, not a skip reported as zero matches:
  the reason reaches a log, and an operator told the control was not found
  goes looking at the form rather than at a frame that never had a context.
* A frame that stops answering *during* the write is
  `FieldWriteNotVerified` — the value may already be in the control, and
  that is the outcome this project never retries.
* Scrolling stays best effort and is merely bounded. The driver's own click
  scrolls again anyway, so giving up on a page whose smooth scroll never
  settles costs nothing.

### 4. One screenshot per application, per attempt

Diagnostics went to `{name}.png`, and the submitter's three names —
`submitted`, `refused`, `unconfirmed` — carried nothing identifying the
application. Every application overwrote the last one's evidence, so a queue
row telling an operator to check a screenshot pointed at a picture of
somebody else's page. For an unconfirmed submission that image is the only
evidence there is.

The submitter now names its images after the thread, like every other
artifact a run writes, and `ArtifactScreenshotter` never overwrites: each
capture gets a timestamped filename, and probes for a free one if two land
in the same microsecond, so one application photographed on two attempts
keeps both. A name is also no longer allowed to decide *where* a file is
written — it is built from a thread id, which comes out of the database.

### 5. The teardown trace that read like a crash

Reading `chromium.executable_path` starts a Playwright driver, and the sync
API's connection is torn down by the interpreter rather than by a running
event loop — leaving a cancelled task and an unretrieved `TargetClosedError`
printed to stderr at the end of every browser run. Deterministic, harmless,
and indistinguishable at a glance from a browser test having died. The
question is asked in a child process now, whose output is ours to discard.

## Mutation testing

Every fix was reverted in place and the tests re-run.

| Mutation | Caught by |
|---|---|
| Freshness by text only (the original bug) | 3 unit tests, and all 5 Chromium regressions |
| Freshness by identity only | the rebuilt-banner unit test |
| Region identity minted per node, ignoring the page's `id` | the Chromium rebuild test, alone |
| Region identity ignoring its own stamp | the Chromium no-`id` test, alone |
| Success destination searched over path and query together | 11 unit tests |
| A silent frame skipped rather than refused while counting | the writer's refusal-reason test |
| Screenshot name without the application | the submitter's naming test |
| Artifact filenames not made unique | the two-attempts test |

Two of the eight are caught **only** in a real browser, which is the answer
to whether the Chromium fixtures earn their 15 seconds: an identity that has
to survive a node being replaced cannot be tested against a double that
never replaces one.

## Concerns

Every concern from the addendum above stands. New:

1. **A banner whose wording the page was already showing elsewhere cannot
   confirm.** The text half of the freshness rule means an ATS that confirms
   with wording identical to a standing panel's records a **failed**
   application that may well have been submitted. It needs the two to match
   exactly after whitespace folding, and the old text-only rule refused the
   same case, so this is not a regression — but it is a false negative kept
   on purpose, because the alternative is the false positive above.
2. **The state script now writes to the page.** It stamps an attribute on
   regions that read like a confirmation, the way the submit target's form is
   already marked. Harmless on every page seen, but it is a mutation made
   while reading, and a page that reacted to attribute changes on its live
   regions would see it.
3. **A region rebuilt with no `id` and different wording is a new region.**
   Both fallbacks are gone in that case: nothing on the node survived, and
   the text changed. A panel that rebuilds itself anonymously *and* rewrites
   its wording each time would still be read as confirming. The Chromium
   fixture covers each of those alone; the combination is not covered because
   it is not a thing a real page does — a rebuilt anonymous node with new
   text is indistinguishable, by any means available in a page, from a
   genuinely new banner.
4. **`TRUTHY_QUERY_VALUES` is a list.** An ATS that signals success with
   `?submitted=Y%20Yes` or a locale-specific word is read as not saying so,
   which is the safe direction and another honest "unconfirmed".
5. **Artifacts accumulate.** Nothing prunes `data/artifacts` now that
   filenames are unique, where before it was self-limiting at a handful of
   files. Retention is an operator's decision, but it is now theirs to make.
6. **The Chromium path probe costs a subprocess.** Once per session, only
   when `CHROME_EXECUTABLE` is unset, about half a second — for a question
   whose answer is a filename. Caching it across sessions would need a file
   nobody has asked for.

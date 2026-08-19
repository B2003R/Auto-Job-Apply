# Task 8 report

## Part 2 — the Important findings

Seven commits, `921e2ae` through `4a7c3ba`, on top of the Task 8 branch
point `6f9f404`. Every change was driven from a failing test except where
noted, and every fix was mutation-checked afterwards.

> Note on this file: the report written at the end of Part 1 was left
> untracked and did not survive the workspace being reset, so this document
> is a complete report rather than an appendix. It is committed now for the
> same reason.

### 1. Authentication could be crashed, and the schema was public

`hmac.compare_digest` raises `TypeError` when either argument is a `str`
containing anything outside ASCII. The presented half of that comparison
came straight off the wire, and Starlette hands header bytes over
latin-1 decoded, so **one accented byte in an `Authorization` header was
an unhandled exception** — a 500 from an unauthenticated caller, and a
traceback in a debug configuration.

The comparison is now on wire bytes. `_wire_bytes` re-encodes with latin-1,
which round-trips exactly what Starlette decoded (latin-1 is a total
mapping, so even byte sequences that are not valid UTF-8 come back
unchanged), and compares against the configured token's UTF-8 encoding —
which is how a token in `.env` reaches the process and how a client sends
one. A side effect worth having: a non-ASCII token now works rather than
never matching.

Nothing caught unexpected exceptions, so a bug answered with Starlette's
bare `Internal Server Error` instead of the envelope every other refusal
uses. There is a catch-all handler now: the caller gets
`{"error": {"kind": "internal_error", "message": ...}}` with a fixed
sentence, and the operator gets the exception and its traceback in the
log. Exception text carries connection strings and paths often enough that
the caller should get none of it — there is a test asserting a fabricated
password in an exception message does not reach the response.

FastAPI mounts `/docs`, `/redoc`, and `/openapi.json` as plain Starlette
routes, so no dependency of ours ran for them: **every route and body
shape was readable by anyone who could reach the port**, including the
non-loopback callers every real route refuses. They are registered by this
project now and carry the same check. With `API_TOKEN` configured they are
not served at all, which is the honest resolution rather than the
convenient one: Swagger UI and ReDoc fetch the schema from the browser,
which cannot attach a bearer token, so an authenticated docs page could
not load the thing it renders, and serving the schema unauthenticated to
make it work would give the whole surface away.

Tests: `TestAuthentication` (non-ASCII presented tokens including invalid
UTF-8, a non-ASCII configured token that must still work, seven malformed
header shapes, and three routes asserted never to echo the token),
`TestUnexpectedFailures`, `TestDocumentationRoutes`.

### 2. A failed local startup leaked the profile lock

`worker.start()` sat outside the local runner's `try`, so a start that
failed *after* the browser was up — which is most of them, since opening
Chrome is the first thing that can go wrong — never reached `stop()`. The
profile lock outlived the process, and every later run, local or served,
refused to start with a message about a lock whose owner was gone. The
operator saw a traceback rather than the sentence `ProfileLockedError` was
written to say.

Startup is guarded; teardown is always attempted and never raises out of
the CLI; a teardown failure on top of a start failure is reported as well
rather than replacing the message. A listing is not queued when the worker
never started, so the queue and the CLI cannot disagree.

Tests: `TestLocalBatch` — a start that fails halfway still closes the
browser, a locked profile is a sentence rather than a traceback, a double
failure still says both things, nothing is queued.

### 3. CSV exports could execute on open

A cell beginning with `=`, `+`, `-`, `@`, a tab, or a carriage return is
executed as a formula by Excel and LibreOffice. Field labels are read off
the listing's own page and answers come from a model, so both are text
this project did not write, and the person opening their own export is the
one whose accounts are worth taking.

Every string cell is escaped with a leading apostrophe at the single point
where a row becomes CSV, so there is no route into the file that bypasses
it. Numbers are left alone, so a negative cost is still a number, and JSON
output is untouched, because the apostrophe is a spreadsheet convention
and would corrupt the value anywhere else.

Tests: six dangerous labels including tab and carriage-return leaders, a
dangerous answer under `--include-values`, an ordinary row asserted
unchanged, and JSON asserted unescaped.

### 4. `data/` was not gitignored

The README said it was. `*.db` covered the default database and nothing
else, so the LangGraph checkpoints and **every screenshot under
`data/artifacts/` — pictures of half-filled application forms, legible
name, address, and salary history — were staged by any `git add -A`**.

The claim is now checked by asking git. Claimed paths are parsed out of
the README prose, so a new promise is verified by the act of making it; a
directory claim is checked through a file inside it, since `data/` is only
a real promise if it covers what is underneath. Three further pins:
the files a default run actually produces, that none of them are tracked
already (an ignore rule does nothing for a file git has), and that the two
example files stay committable, since ignoring too much is its own
failure.

### 5. Exports read the log by opening it for writing

`export_log` called `initialize()`, which opens the database read-write
and applies every schema statement — a read command that could migrate the
thing being read. Each row also came from its own connection, so an export
that walked applications, then queue rows, then fields, then approvals
described four different instants; a worker submitting something part-way
through produced a file describing a combination that never existed.

`Database.read_only_snapshot()` opens `mode=ro`, begins a deferred
transaction, and reads once immediately so the lock is actually taken;
every read inside the block borrows that connection. A write-protected
database — a backup, say — can now be exported at all.

The cost is real and is stated in the docstring, the README, and a test:
on a rollback-journal database a writer's commit waits until the block
ends. Exports are small and the worker's busy timeout is five seconds; a
torn export would be a wrong answer rather than a slow one.

SQLite's wording for the three failures that happen in practice — not a
database, someone else's database, cannot be opened — is replaced with a
sentence naming the path and a next step.

### 6. Smaller items

- **`--local --json` was accepted and ignored**, so a script piping the
  local runner into `jq` got prose and no warning. It emits a named subset
  of each result; the gate payload and gap list are deliberately not in it,
  because `GET /runs/{id}` redacts those according to `LOG_FIELD_VALUES`
  and a second path to the same data is a second path to get it wrong.
- **A `run` that timed out exited 0**, telling a batch script that fifty
  listings were ready for review when they were still staging. It exits 1
  and names the runs; under `--json` the note goes to stderr so the
  document stays parseable.
- **`mypy` added to dev extras.** `types-PyYAML` was already there —
  stubs for a checker the extras did not install — while the README told
  people to run it. A test now asserts every third-party tool the README
  says to run is a declared dev dependency.
- **A production `build_dependencies` safety test.** Every other API test
  replaces the browser-facing components with fakes, so none of them ever
  looked at what a real run gets. `TestTheShippedWiring` fails if the
  submitter or field writer stops raising `ComponentNotWired`, and checks
  the other components are present so it cannot be satisfied by a build
  that does nothing at all.
- **Documentation.** Three things were true of the code and absent from
  the prose. Approving does not submit in this build, which now sits
  directly under the approval rules and in the recovery table.
  `POST /queue` is not idempotent — the one-shot guarantee is per queue
  item, not per listing, which matters most when a client is unsure
  whether a request landed. There is no TLS, so off loopback the token
  crosses the network in clear, and `--token` on a command line is
  readable through `ps auxww`; both have remedies attached. Also
  documented: the export's snapshot and escaping, `--json` in local mode,
  and what the exit codes mean.

### Verification

- **Full suite: 1445 passed** (`python3 -m pytest -q`), up from 1393.
- **mypy: clean** — `python3 -m mypy app scripts --ignore-missing-imports`,
  33 source files.
- **Build: clean** — `python3 -m build` produces both artefacts; the
  selector YAML is in the wheel and `README.md` is in the sdist.
- **Mutation check: 23 of 23 caught.** One mutation initially survived
  because its anchor prefix-matched an earlier function and so mutated the
  wrong line; re-aimed, it was caught. One README mutation survived
  because deleting a heading left the paragraph its test looked for — that
  produced a genuinely new test (every internal `#anchor` must resolve to
  a heading), which was then verified to fail on a renamed heading.

## Part 3 — the two blocking CLI issues

One commit, `4ca5abc`. Both were found by the previous round's own fixes
being incomplete, which is worth saying plainly: Part 2 put teardown after
the `try` rather than inside a `finally`, and routed only *some* of the
local runner's prose away from stdout.

### 1. An interrupt walked past every cleanup path

`KeyboardInterrupt` and `asyncio.CancelledError` derive from
`BaseException`, not `Exception`, and every guard around the local
runner's startup and body caught `Exception`. **Ctrl-C — the way most long
batches actually end — skipped teardown entirely**: the browser stayed
open and the profile lock stayed on disk, so the next run, local or
served, refused to start while naming a pid that no longer existed. The
same held for a `CancelledError` arriving from the outside.

Teardown is now in a `finally` covering both the partial start and the
body, and the interrupt is re-raised after it. Re-raising is the point:
Ctrl-C is the operator taking over, not a failed batch, and folding it
into an exit code leaves a caller unable to tell the two apart.
`_stop_quietly` catches `BaseException` for the matching reason — a
teardown detail, or an impatient second Ctrl-C, must not replace the
exception that started the unwinding.

At the process boundary a new `run()` maps the interrupt to exit 130 and
silence, which is what a shell expects; `main` still re-raises for
anything embedding it.

Tests: `TestInterrupting` — interrupts during startup and during the
batch, both exception types, each asserting the browser was closed; the
re-raise; a teardown failure that must not replace the interrupt; a second
interrupt during teardown that must not replace the first; and both entry
point paths.

### 2. `--local --json` was not machine-readable

A local run narrates — what it queued, what failed, what it is asking at
the gate — and all of it went to stdout alongside the document. **`--local
--json | jq` therefore failed on the first listing**, and the more
interesting the run, the more prose there was to break it. The two tests
that should have caught this parsed `console.lines[-1]`, so they passed on
output that no consumer could read; they parse the whole of stdout now,
which is the only assertion that actually means "machine-readable".

Everything that is not the answer goes through one `note()` helper, which
is `_aside` bound to the mode: stderr under `--json`, stdout otherwise.
That covers the queue narration, startup failures, batch failures,
teardown failures, gate prompts, and the two usage refusals that also
printed to stdout.

A failure leaves stdout **empty** rather than emitting `[]`. An empty list
is a claim that zero runs were requested, and a script that skipped the
exit code would read it as "nothing to do" — the same class of bug as the
timeout exiting 0, which Part 2 fixed.

Tests: `TestLocalJsonStaysMachineReadable`, six cases, each parsing the
entire stdout and asserting the corresponding narration arrived on stderr,
plus a guard that prose mode still narrates.

### Verification

- **Focused: 83 passed** (`tests/scripts/test_cli.py`).
- **Full suite: 1460 passed**, up from 1445.
- **mypy: clean**, 33 source files.
- **Mutation check: 11 of 11 caught** — cleanup moved out of the `finally`,
  the body catching `BaseException`, teardown re-raising over the
  interrupt, a partial start left running, the entry point not handling the
  interrupt, and each of the five prose routes put back on stdout.

### What I would still look at

- **The snapshot blocks writers.** Correct and deliberate, but WAL mode
  would remove the trade-off entirely and is a one-line pragma at
  initialization. It changes the on-disk shape of the database and how it
  behaves on network filesystems, so it did not belong in a fix for this
  finding.
- **The docs are unavailable with a token configured.** The route table in
  the README is the substitute. Serving Swagger UI with the token injected
  into its `fetch` call is possible and is what a follow-up would do; it
  means templating a credential into HTML, which deserves its own thought.
- **`ps auxww` is documented, not solved.** `--token` could read from a
  file descriptor or prompt instead. The environment variable is the
  recommended path and works today, so this is a nicety.
- **`data/` is ignored wholesale.** If someone points `SQLITE_PATH`
  outside it, nothing protects the new location. A pre-commit hook that
  refuses `.db` and `.png` files anywhere would be sturdier than a path
  list.
- **A real cancellation may not permit clean teardown.** The tests raise
  `CancelledError` from inside the coroutine, which leaves the surrounding
  task uncancelled, so the awaits in the `finally` complete normally. A
  loop genuinely shutting down can cancel those awaits too. There is no
  way to close a browser without awaiting something, so the honest answer
  is that a hard cancellation may still leave a lock — which is what
  `doctor` and the lock diagnostics exist for.
- **`--json` prose goes to stderr through `print`, not the injected
  writer.** That is why the tests use `capsys`. It works and it is what a
  process boundary should do, but it means the diagnostics stream is not
  injectable the way stdout is; an embedder wanting both in hand would
  need a second writer parameter.

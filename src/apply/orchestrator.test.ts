import assert from 'node:assert/strict';
import { copyFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { after, before, test } from 'node:test';
import { chromium, type Browser, type BrowserContext } from 'playwright';
import type OpenAI from 'openai';
import { PROJECT_ROOT } from '../config.ts';
import type { JobRow } from '../store/db.ts';
import type { ApplyClickResult, JobSource } from '../sources/types.ts';

const dataDir = mkdtempSync(join(tmpdir(), 'orchestrator-test-'));
process.env.OPENAI_API_KEY ??= 'test-key';
process.env.APPLICATION_EMAIL ??= 'ada@example.com';
process.env.AGENT_DATA_DIR = dataDir;

const { closeDb, getDb, upsertJob } = await import('../store/db.ts');
const { resetAnswerBankCache } = await import('./answerBank.ts');
const { setOpenAIClient } = await import('../llm/client.ts');
const { applyToJob } = await import('./orchestrator.ts');
const { pageKinds } = await import('../llm/schemas.ts');

const profilePath = resolve(PROJECT_ROOT, 'config/profile.yaml');
const answersPath = resolve(PROJECT_ROOT, 'config/answers.yaml');

let browser: Browser;
let context: BrowserContext;

before(async () => {
  copyFileSync(resolve(PROJECT_ROOT, 'config/profile.example.yaml'), profilePath);
  copyFileSync(resolve(PROJECT_ROOT, 'config/answers.example.yaml'), answersPath);
  resetAnswerBankCache();
  browser = await chromium.launch();
  context = await browser.newContext();
});

after(async () => {
  await browser?.close();
  closeDb();
  rmSync(dataDir, { recursive: true, force: true });
  rmSync(profilePath, { force: true });
  rmSync(answersPath, { force: true });
  resetAnswerBankCache();
  setOpenAIClient(null);
});

/**
 * Routes model calls by purpose so one stub can serve the classifier, the
 * verifier and the confirmation check within a single run.
 */
interface ModelCallCounts {
  classify: number;
  verify: number;
  confirm: number;
}

function stubModel(handlers: {
  classify?: () => unknown;
  verify?: () => unknown;
  confirm?: () => unknown;
}): { calls: ModelCallCounts } {
  const calls: ModelCallCounts = { classify: 0, verify: 0, confirm: 0 };

  setOpenAIClient({
    chat: {
      completions: {
        create: async (body: { messages: Array<{ content: unknown }> }) => {
          const system = String(body.messages[0]?.content ?? '');
          let payload: unknown;

          if (system.includes('whether a job application was actually submitted')) {
            calls.confirm += 1;
            payload = handlers.confirm?.() ?? { kind: 'confirmation', confidence: 0.95, evidence: 'Application submitted' };
          } else if (system.includes('identify what kind of page')) {
            calls.classify += 1;
            payload = handlers.classify?.() ?? { kind: 'application_form', confidence: 0.9, evidence: 'form' };
          } else {
            calls.verify += 1;
            payload = handlers.verify?.() ?? {
              status: 'ready_to_submit',
              blockers: [],
              unanswerable: [],
              fixes: [],
              notes: '',
            };
          }

          return { choices: [{ message: { content: JSON.stringify(payload) } }], usage: {} };
        },
      },
    },
  } as unknown as OpenAI);

  return { calls };
}

/** A source whose apply click serves a canned page, standing in for a real board. */
function stubSource(html: string, overrides: Partial<JobSource> = {}): JobSource {
  return {
    key: 'jobright',
    label: 'Jobright',
    collect: async () => [],
    clickApply: async (ctx): Promise<ApplyClickResult> => {
      const page = await ctx.newPage();
      await page.route('**/apply*', (route) => route.fulfill({ contentType: 'text/html', body: html }));
      await page.goto('https://boards.greenhouse.io/acme/apply');
      return { kind: 'navigated', page, note: 'followed apply' };
    },
    ...overrides,
  };
}

function queueJob(url: string, title: string): JobRow {
  const { id } = upsertJob({ source: 'jobright', url, company: 'Acme', title });
  return getDb().prepare('SELECT * FROM jobs WHERE id = ?').get(id) as JobRow;
}

const COMPLETE_FORM = `<!doctype html><html><head><title>Apply</title></head><body>
  <h1>Apply for Senior Engineer</h1>
  <form>
    <label for="first">First name *</label><input id="first" required value="Ada" />
    <label for="last">Last name *</label><input id="last" required value="Lovelace" />
    <label for="email">Email *</label><input id="email" type="email" required value="ada@example.com" />
    <label for="phone">Phone *</label><input id="phone" type="tel" required value="+1 555 010 0100" />
    <label for="terms">I accept the terms</label><input id="terms" type="checkbox" required checked />
    <button type="submit">Submit Application</button>
  </form>
</body></html>`;

const CLOSED_POSTING = `<!doctype html><html><head><title>Closed</title></head><body>
  <h1>Role unavailable</h1><p>This job is no longer available.</p>
</body></html>`;

const CAPTCHA_PAGE = `<!doctype html><html><head><title>Verify</title></head><body>
  <h1>Security check</h1><p>Please verify you are human to continue.</p>
</body></html>`;

test('submits a complete form and records a confirmed submission', async () => {
  stubModel({});
  const job = queueJob('https://jobright.ai/jobs/e2e-1', 'Senior Engineer');

  const result = await applyToJob(context, stubSource(COMPLETE_FORM), job, { mode: 'auto', interactive: false });

  assert.equal(result.finalState, 'submitted');
  assert.equal(result.failureClass, null);
  assert.equal(result.atsVendor, 'greenhouse');

  const row = getDb().prepare('SELECT status FROM jobs WHERE id = ?').get(job.id) as { status: string };
  assert.equal(row.status, 'submitted');

  const counter = getDb()
    .prepare(`SELECT submitted FROM daily_counters WHERE source = 'jobright'`)
    .get() as { submitted: number };
  assert.ok(counter.submitted >= 1, 'the daily counter must advance so the 50/day target is enforceable');
});

test('dry-run fills and verifies but leaves the job queued for a real run', async () => {
  stubModel({});
  const job = queueJob('https://jobright.ai/jobs/e2e-2', 'Backend Engineer');

  const result = await applyToJob(context, stubSource(COMPLETE_FORM), job, { mode: 'dry-run', interactive: false });

  assert.equal(result.finalState, 'dry_run_complete');
  const row = getDb().prepare('SELECT status FROM jobs WHERE id = ?').get(job.id) as { status: string };
  assert.equal(row.status, 'queued', 'a dry run must not consume the job');
});

test('reports an unconfirmed submission as needing a human rather than as success', async () => {
  // Clicking submit is not proof it went through; the operator must be told which
  // ones to check by hand.
  stubModel({ confirm: () => ({ kind: 'other', confidence: 0.2, evidence: 'Page still shows the form' }) });
  const job = queueJob('https://jobright.ai/jobs/e2e-3', 'Platform Engineer');

  const result = await applyToJob(context, stubSource(COMPLETE_FORM), job, { mode: 'auto', interactive: false });

  assert.equal(result.finalState, 'blocked_human');
  assert.equal(result.failureClass, 'submit_unconfirmed');
});

test('skips a closed posting without calling the model', async () => {
  const model = stubModel({});
  const job = queueJob('https://jobright.ai/jobs/e2e-4', 'Closed Role');

  const result = await applyToJob(context, stubSource(CLOSED_POSTING), job, { mode: 'auto', interactive: false });

  assert.equal(result.finalState, 'skipped');
  assert.equal(result.failureClass, 'posting_closed');
  assert.equal(model.calls.classify, 0, 'a closed posting is detected by heuristics alone');
});

test('blocks on a CAPTCHA and records the URL for the daily export', async () => {
  stubModel({});
  const job = queueJob('https://jobright.ai/jobs/e2e-5', 'Guarded Role');

  const result = await applyToJob(context, stubSource(CAPTCHA_PAGE), job, { mode: 'auto', interactive: false });

  assert.equal(result.finalState, 'blocked_human');
  assert.equal(result.failureClass, 'captcha');
  assert.ok(result.finalUrl.includes('greenhouse.io'), 'the URL must be retained so it can be exported');
});

test('refuses to submit when the verifier says a required question is unanswerable', async () => {
  stubModel({
    verify: () => ({
      status: 'needs_human',
      blockers: ['A required question has no answer in the bank'],
      unanswerable: [{ fieldId: 'f1', question: 'Describe your proudest achievement' }],
      fixes: [],
      notes: '',
    }),
  });
  const job = queueJob('https://jobright.ai/jobs/e2e-6', 'Essay Role');

  const result = await applyToJob(context, stubSource(COMPLETE_FORM), job, { mode: 'auto', interactive: false });

  assert.equal(result.finalState, 'blocked_human');
  assert.equal(result.failureClass, 'unanswered_question');

  const recorded = getDb()
    .prepare('SELECT question FROM unanswered_questions WHERE question LIKE ?')
    .get('%proudest%') as { question: string } | undefined;
  assert.ok(recorded, 'the question must be recorded so the answer bank can be extended');
});

test('records a failure rather than throwing when apply is unavailable', async () => {
  stubModel({});
  const job = queueJob('https://jobright.ai/jobs/e2e-7', 'Broken Role');
  const source = stubSource(COMPLETE_FORM, {
    clickApply: async () => ({ kind: 'unavailable', note: 'No apply control found' }),
  });

  const result = await applyToJob(context, source, job, { mode: 'auto', interactive: false });

  assert.equal(result.finalState, 'failed');
  assert.equal(result.failureClass, 'apply_unavailable');
});

test('one broken posting cannot end the run', async () => {
  stubModel({});
  const job = queueJob('https://jobright.ai/jobs/e2e-8', 'Exploding Role');
  const source = stubSource(COMPLETE_FORM, {
    clickApply: async () => {
      throw new Error('navigation exploded');
    },
  });

  const result = await applyToJob(context, source, job, { mode: 'auto', interactive: false });

  assert.equal(result.finalState, 'failed');
  assert.equal(result.failureClass, 'error');
  assert.match(result.detail, /navigation exploded/);
});

test('writes a resumable event trail for every attempt', () => {
  const events = getDb()
    .prepare('SELECT DISTINCT state FROM events')
    .all() as Array<{ state: string }>;
  const states = new Set(events.map((e) => e.state));
  for (const expected of ['OPENING_JOB', 'ON_ATS', 'CLASSIFIED', 'AUTOFILLING', 'VERIFIED']) {
    assert.ok(states.has(expected), `expected a ${expected} event to be journalled`);
  }
});

test('the classifier schema and the page kinds stay in step', () => {
  assert.ok(pageKinds.includes('application_form'));
  assert.ok(pageKinds.includes('captcha'));
});

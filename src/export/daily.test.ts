import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';

const dataDir = mkdtempSync(join(tmpdir(), 'export-test-'));
process.env.OPENAI_API_KEY ??= 'test-key';
process.env.APPLICATION_EMAIL ??= 'ada@example.com';
process.env.AGENT_DATA_DIR = dataDir;

const { closeDb, finishAttempt, getDb, recordArtifact, recordUnansweredQuestion, startAttempt, today, upsertJob } =
  await import('../store/db.ts');
const { toCsv, writeDailyExport } = await import('./daily.ts');

let exportDir: string;

before(() => {
  const submittedJob = upsertJob({
    source: 'jobright',
    url: 'https://boards.greenhouse.io/acme/jobs/1',
    company: 'Acme',
    title: 'Senior Engineer',
    location: 'Remote',
  });
  const submittedAttempt = startAttempt(submittedJob.id, 'auto');
  finishAttempt(submittedAttempt, {
    finalState: 'submitted',
    submitted: true,
    atsVendor: 'greenhouse',
    finalUrl: 'https://boards.greenhouse.io/acme/confirmation',
    stepsCompleted: 2,
  });

  const blockedJob = upsertJob({
    source: 'linkedin',
    url: 'https://www.linkedin.com/jobs/view/2',
    company: 'Globex, Inc.',
    title: 'Staff Engineer, "Platform"',
  });
  const blockedAttempt = startAttempt(blockedJob.id, 'auto');
  recordArtifact('captcha', '/tmp/shot.png', { jobId: blockedJob.id, attemptId: blockedAttempt });
  finishAttempt(blockedAttempt, {
    finalState: 'blocked_human',
    submitted: false,
    atsVendor: 'workday',
    finalUrl: 'https://globex.wd1.myworkdayjobs.com/apply',
    failureClass: 'captcha',
    failureMessage: 'Challenge still present after human intervention',
    stepsCompleted: 1,
  });

  const dryJob = upsertJob({ source: 'wellfound', url: 'https://wellfound.com/jobs/3', company: 'Initech', title: 'SRE' });
  const dryAttempt = startAttempt(dryJob.id, 'dry-run');
  finishAttempt(dryAttempt, {
    finalState: 'dry_run_complete',
    submitted: false,
    finalUrl: 'https://jobs.lever.co/initech/x',
    stepsCompleted: 1,
  });

  recordUnansweredQuestion({ question: 'Describe your proudest achievement', fieldType: 'textarea' });

  exportDir = writeDailyExport(today()).dir;
});

after(() => {
  closeDb();
  rmSync(dataDir, { recursive: true, force: true });
});

const read = (name: string): string => readFileSync(join(exportDir, name), 'utf8');

test('quotes CSV fields containing commas and doubles inner quotes', () => {
  const csv = toCsv([
    ['a', 'b'],
    ['Globex, Inc.', 'Staff Engineer, "Platform"'],
  ]);
  assert.match(csv, /"Globex, Inc\."/);
  assert.match(csv, /"Staff Engineer, ""Platform"""/);
});

test('flattens newlines so one attempt stays on one CSV row', () => {
  const csv = toCsv([['x'], ['line one\nline two']]);
  assert.equal(csv.trim().split('\n').length, 2);
});

test('applications.csv contains every attempt', () => {
  const csv = read('applications.csv');
  assert.match(csv, /Senior Engineer/);
  assert.match(csv, /Staff Engineer/);
  assert.match(csv, /SRE/);
});

test('failures.csv carries the URL, stage and screenshot for what did not finish', () => {
  const csv = read('failures.csv');
  assert.match(csv, /captcha/);
  assert.match(csv, /globex\.wd1\.myworkdayjobs\.com/);
  assert.match(csv, /shot\.png/);
});

test('failures.csv excludes submitted applications', () => {
  assert.doesNotMatch(read('failures.csv'), /Senior Engineer/);
});

test('a dry-run is not reported as a failure', () => {
  // Dry-run deliberately does not submit; treating it as a failure would bury the
  // real problems in noise.
  assert.doesNotMatch(read('failures.csv'), /Initech|SRE/);
});

test('failed-urls.txt is a bare pasteable list', () => {
  const urls = read('failed-urls.txt').trim().split('\n');
  assert.deepEqual(urls, ['https://globex.wd1.myworkdayjobs.com/apply']);
});

test('summary.md reports counts, reasons and questions to add', () => {
  const summary = read('summary.md');
  assert.match(summary, /Submitted: 1/);
  assert.match(summary, /Needing attention: 1/);
  assert.match(summary, /captcha/);
  assert.match(summary, /Describe your proudest achievement/);
});

test('an export runs cleanly for a day with no activity', () => {
  const result = writeDailyExport('2020-01-01');
  assert.equal(result.submitted, 0);
  assert.equal(result.unfinished, 0);
  assert.equal(readFileSync(join(result.dir, 'failed-urls.txt'), 'utf8'), '');
});

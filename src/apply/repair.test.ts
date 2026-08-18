import assert from 'node:assert/strict';
import { copyFileSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { after, before, test } from 'node:test';
import { chromium, type Browser, type Page } from 'playwright';
import type OpenAI from 'openai';
import { PROJECT_ROOT } from '../config.ts';
import { resetAnswerBankCache } from './answerBank.ts';
import { extractFormSnapshot, type FormSnapshot } from './formExtractor.ts';
import { applyDeterministicAnswers, sanitiseFixes, verifyAndRepair } from './repair.ts';
import { setOpenAIClient } from '../llm/client.ts';
import type { RepairAction, Verdict } from '../llm/schemas.ts';
import type { AutofillResult } from '../jobright/autofill.ts';

process.env.OPENAI_API_KEY ??= 'test-key';
process.env.APPLICATION_EMAIL ??= 'ada@example.com';

const html = readFileSync(join(import.meta.dirname, 'fixtures/ats-form.html'), 'utf8');
const profilePath = resolve(PROJECT_ROOT, 'config/profile.yaml');
const answersPath = resolve(PROJECT_ROOT, 'config/answers.yaml');

let browser: Browser;

before(async () => {
  copyFileSync(resolve(PROJECT_ROOT, 'config/profile.example.yaml'), profilePath);
  copyFileSync(resolve(PROJECT_ROOT, 'config/answers.example.yaml'), answersPath);
  resetAnswerBankCache();
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
  rmSync(profilePath, { force: true });
  rmSync(answersPath, { force: true });
  resetAnswerBankCache();
  setOpenAIClient(null);
});

async function freshPage(): Promise<Page> {
  const page = await browser.newPage();
  await page.setContent(html);
  return page;
}

/** Minimal OpenAI stand-in that replays a queue of verdicts, repeating the last. */
function stubModel(verdicts: Verdict[]): { calls: number } {
  const state = { calls: 0 };
  const queue = [...verdicts];
  let last = verdicts.at(-1);
  setOpenAIClient({
    chat: {
      completions: {
        create: async () => {
          state.calls += 1;
          const next = queue.shift() ?? last;
          last = next;
          return {
            choices: [{ message: { content: JSON.stringify(next) } }],
            usage: { prompt_tokens: 1, completion_tokens: 1 },
          };
        },
      },
    },
  } as unknown as OpenAI);
  return state;
}

const verdict = (overrides: Partial<Verdict>): Verdict => ({
  status: 'ready_to_submit',
  blockers: [],
  unanswerable: [],
  fixes: [],
  notes: '',
  ...overrides,
});

const autofillStub: AutofillResult = {
  triggered: true,
  filled: 3,
  required: 6,
  progressSource: 'panel',
  fieldsWithValues: 3,
  blockedReason: null,
  complete: false,
};

// --- The safety boundary -----------------------------------------------------

function snapshotFixture(): FormSnapshot {
  return {
    url: 'https://ats.example.com/apply',
    title: 'Apply',
    headings: [],
    errorBanners: [],
    fields: [
      { id: 'f1', label: 'First name', kind: 'text', value: '', required: true, disabled: false },
      { id: 'f2', label: 'Locked', kind: 'text', value: '', required: false, disabled: true },
      {
        id: 'f3',
        label: 'Authorized to work',
        kind: 'select',
        value: '',
        required: true,
        disabled: false,
        options: ['Yes', 'No'],
      },
    ],
    buttons: [
      { id: 'b1', label: 'Continue', kind: 'next', disabled: false },
      { id: 'b2', label: 'Submit Application', kind: 'submit', disabled: false },
      { id: 'b3', label: 'Next', kind: 'next', disabled: true },
    ],
  };
}

test('refuses a submit button: submitting is never the model\'s decision', () => {
  const fixes: RepairAction[] = [{ action: 'click_button', buttonId: 'b2', reason: 'finish' }];
  const { allowed, rejected } = sanitiseFixes(fixes, snapshotFixture());
  assert.equal(allowed.length, 0);
  assert.match(rejected[0] ?? '', /submit control/);
});

test('refuses actions on fields that were never in the snapshot', () => {
  const fixes: RepairAction[] = [
    { action: 'set_text', fieldId: 'f99', value: 'x', reason: 'invented' },
    { action: 'click_button', buttonId: 'b99', reason: 'invented' },
  ];
  const { allowed, rejected } = sanitiseFixes(fixes, snapshotFixture());
  assert.equal(allowed.length, 0);
  assert.equal(rejected.length, 2);
  assert.ok(rejected.every((r) => /unknown/.test(r)));
});

test('refuses select values that were never offered as options', () => {
  const fixes: RepairAction[] = [
    { action: 'select_option', fieldId: 'f3', value: 'Maybe', reason: 'guess' },
  ];
  const { allowed, rejected } = sanitiseFixes(fixes, snapshotFixture());
  assert.equal(allowed.length, 0);
  assert.match(rejected[0] ?? '', /not an option/);
});

test('normalises a case-mismatched option to the exact offered string', () => {
  const fixes: RepairAction[] = [
    { action: 'select_option', fieldId: 'f3', value: 'yes', reason: 'work authorization' },
  ];
  const { allowed } = sanitiseFixes(fixes, snapshotFixture());
  assert.equal(allowed.length, 1);
  assert.equal(allowed[0]?.action === 'select_option' ? allowed[0].value : null, 'Yes');
});

test('refuses disabled fields and disabled buttons', () => {
  const fixes: RepairAction[] = [
    { action: 'set_text', fieldId: 'f2', value: 'x', reason: 'locked field' },
    { action: 'click_button', buttonId: 'b3', reason: 'disabled next' },
  ];
  const { allowed, rejected } = sanitiseFixes(fixes, snapshotFixture());
  assert.equal(allowed.length, 0);
  assert.equal(rejected.length, 2);
  assert.ok(rejected.every((r) => /disabled/.test(r)));
});

test('allows a legitimate fix and a legitimate next click', () => {
  const fixes: RepairAction[] = [
    { action: 'set_text', fieldId: 'f1', value: 'Ada', reason: 'profile.identity.firstName' },
    { action: 'click_button', buttonId: 'b1', reason: 'advance' },
  ];
  const { allowed, rejected } = sanitiseFixes(fixes, snapshotFixture());
  assert.equal(allowed.length, 2);
  assert.equal(rejected.length, 0);
});

// --- Filling real controls ---------------------------------------------------

test('fills required text, select, checkbox and radio controls from the answer bank', async () => {
  const page = await freshPage();
  try {
    const snapshot = await extractFormSnapshot(page);
    const { filled } = await applyDeterministicAnswers(page, snapshot);
    assert.ok(filled >= 3, `expected several fields filled, got ${filled}`);

    assert.equal(await page.locator('input[name="last_name"]').inputValue(), 'Lovelace');
    assert.equal(await page.locator('#auth').inputValue(), 'yes', 'select should resolve Yes to its option');
    assert.equal(await page.locator('#terms').isChecked(), true, 'required checkbox should be accepted');
  } finally {
    await page.close();
  }
});

test('leaves an acknowledgement checkbox untouched when consent is not granted', async () => {
  const withoutConsent = readFileSync(resolve(PROJECT_ROOT, 'config/answers.example.yaml'), 'utf8').replace(
    'acceptTermsAndPolicies: true',
    'acceptTermsAndPolicies: false',
  );
  writeFileSync(answersPath, withoutConsent);
  resetAnswerBankCache();

  const page = await freshPage();
  try {
    const snapshot = await extractFormSnapshot(page);
    await applyDeterministicAnswers(page, snapshot);
    assert.equal(
      await page.locator('#terms').isChecked(),
      false,
      'consent must not be given on the operator\'s behalf without the explicit flag',
    );
  } finally {
    await page.close();
    copyFileSync(resolve(PROJECT_ROOT, 'config/answers.example.yaml'), answersPath);
    resetAnswerBankCache();
  }
});

test('does not overwrite a field that already holds a value', async () => {
  const page = await freshPage();
  try {
    const snapshot = await extractFormSnapshot(page);
    await applyDeterministicAnswers(page, snapshot);
    assert.equal(await page.locator('#first').inputValue(), 'Ada');
  } finally {
    await page.close();
  }
});

// --- The repair loop ---------------------------------------------------------

test('stops as soon as the verifier reports the form is ready', async () => {
  const page = await freshPage();
  const model = stubModel([verdict({ status: 'ready_to_submit' })]);
  try {
    const outcome = await verifyAndRepair(page, { autofill: autofillStub });
    assert.equal(outcome.status, 'ready_to_submit');
    assert.equal(outcome.loops, 1);
    assert.equal(model.calls, 1, 'a ready verdict must not trigger further model calls');
  } finally {
    await page.close();
  }
});

test('applies fixes, re-checks, and reports the resulting status', async () => {
  const page = await freshPage();
  const model = stubModel([
    verdict({
      status: 'needs_fixes',
      blockers: ['Phone number is empty'],
      fixes: [{ action: 'set_text', fieldId: 'f4', value: '+1 555 010 0100', reason: 'profile.identity.phone' }],
    }),
    verdict({ status: 'ready_to_submit' }),
  ]);
  try {
    const outcome = await verifyAndRepair(page, { autofill: autofillStub });
    assert.equal(outcome.status, 'ready_to_submit');
    assert.equal(model.calls, 2, 'should re-verify after applying fixes');
    assert.ok(outcome.appliedActions > 0);
  } finally {
    await page.close();
  }
});

test('surfaces a next-step request with the button to click, without clicking submit', async () => {
  const page = await freshPage();
  stubModel([
    verdict({
      status: 'needs_next_step',
      fixes: [{ action: 'click_button', buttonId: 'b2', reason: 'advance' }],
    }),
  ]);
  try {
    const outcome = await verifyAndRepair(page, { autofill: autofillStub });
    assert.equal(outcome.status, 'needs_next_step');
    assert.equal(outcome.nextButtonId, 'b2');
  } finally {
    await page.close();
  }
});

test('records unanswerable questions so the operator can extend the answer bank', async () => {
  const page = await freshPage();
  const seen: string[] = [];
  stubModel([
    verdict({
      status: 'needs_human',
      blockers: ['A required question is not covered by the answer bank'],
      unanswerable: [{ fieldId: 'f11', question: 'Describe your proudest technical achievement' }],
    }),
  ]);
  try {
    const outcome = await verifyAndRepair(page, {
      autofill: autofillStub,
      onUnanswerable: (question) => seen.push(question),
    });
    assert.equal(outcome.status, 'needs_human');
    assert.deepEqual(seen, ['Describe your proudest technical achievement']);
    assert.equal(outcome.unanswerable.length, 1);
  } finally {
    await page.close();
  }
});

test('gives up after the configured number of loops instead of looping forever', async () => {
  const page = await freshPage();
  const model = stubModel([
    verdict({
      status: 'needs_fixes',
      fixes: [{ action: 'set_text', fieldId: 'f4', value: '+1 555 010 0100', reason: 'phone' }],
    }),
  ]);
  try {
    const outcome = await verifyAndRepair(page, { autofill: autofillStub });
    assert.equal(outcome.status, 'needs_fixes');
    assert.equal(outcome.loops, 3, 'should stop at safety.maxRepairLoops');
    assert.equal(model.calls, 3);
  } finally {
    await page.close();
  }
});

test('stops when every requested fix is rejected rather than burning the loop budget', async () => {
  const page = await freshPage();
  const model = stubModel([
    verdict({
      status: 'needs_fixes',
      fixes: [{ action: 'set_text', fieldId: 'nonexistent', value: 'x', reason: 'invented' }],
    }),
  ]);
  try {
    const outcome = await verifyAndRepair(page, { autofill: autofillStub });
    assert.equal(model.calls, 1, 'no point re-verifying when nothing could be applied');
    assert.equal(outcome.rejectedActions.length, 1);
  } finally {
    await page.close();
  }
});

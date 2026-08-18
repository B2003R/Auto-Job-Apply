import assert from 'node:assert/strict';
import { test } from 'node:test';
import { decideSubmission } from './submit.ts';
import type { FormSnapshot } from './formExtractor.ts';
import type { AutofillResult } from '../jobright/autofill.ts';
import type { RunMode } from '../config.ts';

process.env.OPENAI_API_KEY ??= 'test-key';
process.env.APPLICATION_EMAIL ??= 'ada@example.com';

const completeAutofill: AutofillResult = {
  triggered: true,
  filled: 4,
  required: 4,
  progressSource: 'panel',
  fieldsWithValues: 4,
  blockedReason: null,
  complete: true,
};

function snapshot(overrides: Partial<FormSnapshot> = {}): FormSnapshot {
  return {
    url: 'https://ats.example.com/apply',
    title: 'Apply',
    headings: [],
    errorBanners: [],
    fields: [
      { id: 'f1', label: 'First name', kind: 'text', value: 'Ada', required: true, disabled: false },
      { id: 'f2', label: 'Email', kind: 'email', value: 'ada@example.com', required: true, disabled: false },
    ],
    buttons: [{ id: 'b1', label: 'Submit Application', kind: 'submit', disabled: false }],
    ...overrides,
  };
}

const gate = (over: Partial<Parameters<typeof decideSubmission>[0]> = {}) =>
  decideSubmission({
    verdictStatus: 'ready_to_submit',
    autofill: completeAutofill,
    snapshot: snapshot(),
    mode: 'auto' as RunMode,
    ...over,
  });

test('submits when the verifier is ready and no required field is empty', () => {
  assert.deepEqual(gate(), { submit: true });
});

test('refuses when the verifier is not ready, whatever the form looks like', () => {
  for (const status of ['needs_fixes', 'needs_next_step', 'not_an_application_form'] as const) {
    const decision = gate({ verdictStatus: status });
    assert.equal(decision.submit, false, `${status} must not submit`);
  }
});

test('flags needs_human for a human-required verdict', () => {
  const decision = gate({ verdictStatus: 'needs_human' });
  assert.equal(decision.submit, false);
  assert.equal(decision.submit === false && decision.needsHuman, true);
});

test('refuses a ready verdict when a required field is still empty', () => {
  // The independent mechanical check is what catches a confidently wrong verdict.
  const decision = gate({
    snapshot: snapshot({
      fields: [
        { id: 'f1', label: 'First name', kind: 'text', value: 'Ada', required: true, disabled: false },
        { id: 'f2', label: 'Work authorization', kind: 'select', value: '', required: true, disabled: false },
      ],
    }),
  });
  assert.equal(decision.submit, false);
  assert.match(decision.submit === false ? decision.reason : '', /Work authorization/);
});

test('refuses when a field still shows a validation error', () => {
  const decision = gate({
    snapshot: snapshot({
      fields: [
        {
          id: 'f1',
          label: 'Phone',
          kind: 'tel',
          value: '123',
          required: true,
          disabled: false,
          error: 'Enter a valid phone number',
        },
      ],
    }),
  });
  assert.equal(decision.submit, false);
  assert.match(decision.submit === false ? decision.reason : '', /valid phone number/);
});

test('refuses when the extension reports required fields outstanding', () => {
  const decision = gate({
    autofill: { ...completeAutofill, filled: 2, required: 5, complete: false },
  });
  assert.equal(decision.submit, false);
  assert.match(decision.submit === false ? decision.reason : '', /2 of 5/);
});

test('proceeds when the extension gave no progress readout at all', () => {
  // A missing readout is an absence of evidence, not evidence of a problem; the
  // required-field check already covers correctness.
  const decision = gate({
    autofill: { ...completeAutofill, filled: null, required: null, progressSource: 'none', triggered: false },
  });
  assert.deepEqual(decision, { submit: true });
});

test('refuses when there is no enabled submit control', () => {
  const decision = gate({
    snapshot: snapshot({ buttons: [{ id: 'b1', label: 'Submit', kind: 'submit', disabled: true }] }),
  });
  assert.equal(decision.submit, false);
  assert.match(decision.submit === false ? decision.reason : '', /No enabled submit control/);
});

test('dry-run never submits, and does not report it as needing a human', () => {
  const decision = gate({ mode: 'dry-run' });
  assert.equal(decision.submit, false);
  assert.equal(decision.submit === false && decision.needsHuman, false);
  assert.match(decision.submit === false ? decision.reason : '', /dry-run/);
});

test('an unchecked required checkbox counts as empty', () => {
  const decision = gate({
    snapshot: snapshot({
      fields: [
        { id: 'f1', label: 'I accept the terms', kind: 'checkbox', value: 'false', required: true, disabled: false },
      ],
    }),
  });
  assert.equal(decision.submit, false);
  assert.match(decision.submit === false ? decision.reason : '', /I accept the terms/);
});

test('ignores disabled required fields, which cannot be filled anyway', () => {
  const decision = gate({
    snapshot: snapshot({
      fields: [
        { id: 'f1', label: 'First name', kind: 'text', value: 'Ada', required: true, disabled: false },
        { id: 'f2', label: 'Computed field', kind: 'text', value: '', required: true, disabled: true },
      ],
    }),
  });
  assert.deepEqual(decision, { submit: true });
});

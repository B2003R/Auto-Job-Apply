import assert from 'node:assert/strict';
import { test } from 'node:test';
import { classifyByHeuristics, detectAtsVendor } from './pageClassifier.ts';
import type { FormSnapshot } from './formExtractor.ts';

process.env.OPENAI_API_KEY ??= 'test-key';
process.env.APPLICATION_EMAIL ??= 'ada@example.com';

function snapshot(overrides: Partial<FormSnapshot> = {}): FormSnapshot {
  return {
    url: 'https://ats.example.com/apply',
    title: 'Apply',
    headings: [],
    errorBanners: [],
    fields: [],
    buttons: [],
    ...overrides,
  };
}

const field = (label: string, kind: FormSnapshot['fields'][number]['kind'] = 'text') => ({
  id: `f${Math.random().toString(36).slice(2, 7)}`,
  label,
  kind,
  value: '',
  required: false,
  disabled: false,
});

test('identifies ATS vendors from the host', () => {
  assert.equal(detectAtsVendor('https://boards.greenhouse.io/acme/jobs/123'), 'greenhouse');
  assert.equal(detectAtsVendor('https://jobs.lever.co/acme/abc'), 'lever');
  assert.equal(detectAtsVendor('https://acme.wd1.myworkdayjobs.com/en-US/careers'), 'workday');
  assert.equal(detectAtsVendor('https://acme.ashbyhq.com/x'), 'ashby');
  assert.equal(detectAtsVendor('https://careers.acme.com/apply'), null);
  assert.equal(detectAtsVendor('not a url'), null);
});

test('treats a CAPTCHA as overriding every other page signal', () => {
  // A page can look like a form and still be a bot wall; the wall wins.
  const result = classifyByHeuristics(
    'https://ats.example.com/apply',
    snapshot({
      fields: [field('First name'), field('Last name'), field('Email'), field('Phone'), field('Resume', 'file')],
      buttons: [{ id: 'b1', label: 'Submit', kind: 'submit', disabled: false }],
    }),
    'Please verify you are human before continuing',
  );
  assert.equal(result?.kind, 'captcha');
});

test('detects a closed posting', () => {
  const result = classifyByHeuristics(
    'https://ats.example.com/apply',
    snapshot(),
    'This job is no longer available.',
  );
  assert.equal(result?.kind, 'expired_or_closed');
});

test('requires a confirmation page to have no form left on it', () => {
  const withForm = classifyByHeuristics(
    'https://ats.example.com/apply',
    snapshot({
      fields: [field('First name'), field('Last name'), field('Email')],
      buttons: [{ id: 'b1', label: 'Submit', kind: 'submit', disabled: false }],
    }),
    'Thank you for applying. Please complete the form below.',
  );
  assert.notEqual(withForm?.kind, 'confirmation', 'a page still showing a form is not a confirmation');

  const withoutForm = classifyByHeuristics(
    'https://ats.example.com/apply',
    snapshot(),
    'Your application has been submitted.',
  );
  assert.equal(withoutForm?.kind, 'confirmation');
});

test('distinguishes signup from login by the second password field', () => {
  const signup = classifyByHeuristics(
    'https://ats.example.com/register',
    snapshot({ fields: [field('Email'), field('Password'), field('Confirm Password')] }),
    'Create an account to continue',
  );
  assert.equal(signup?.kind, 'signup_required');

  const login = classifyByHeuristics(
    'https://ats.example.com/login',
    snapshot({ fields: [field('Email'), field('Password')] }),
    'Sign in to your account',
  );
  assert.equal(login?.kind, 'login_required');
});

test('detects an email verification wall', () => {
  const result = classifyByHeuristics(
    'https://ats.example.com/verify',
    snapshot({ fields: [field('Verification code')] }),
    'We sent you a code. Please check your email.',
  );
  assert.equal(result?.kind, 'email_verification_required');
});

test('recognises a substantial form as the application', () => {
  const result = classifyByHeuristics(
    'https://boards.greenhouse.io/acme/jobs/1',
    snapshot({
      fields: [field('First name'), field('Last name'), field('Email'), field('Phone'), field('Resume', 'file')],
      buttons: [{ id: 'b1', label: 'Submit Application', kind: 'submit', disabled: false }],
    }),
    'Apply for this job',
  );
  assert.equal(result?.kind, 'application_form');
});

test('defers to the model rather than guessing on a thin, ambiguous page', () => {
  const result = classifyByHeuristics(
    'https://careers.example.com/job/1',
    snapshot({ fields: [field('Search')], buttons: [{ id: 'b1', label: 'Go', kind: 'other', disabled: false }] }),
    'Software Engineer. We are hiring.',
  );
  assert.equal(result, null);
});

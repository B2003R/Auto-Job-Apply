import assert from 'node:assert/strict';
import { copyFileSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { after, before, test } from 'node:test';
import { PROJECT_ROOT } from '../config.ts';
import { loadAnswerBank, lookupAnswer, resetAnswerBankCache, type AnswerBank } from './answerBank.ts';

// The loader reads fixed paths under config/, so the examples are copied into
// place for the duration of the suite and any pre-existing files are preserved.
const profilePath = resolve(PROJECT_ROOT, 'config/profile.yaml');
const answersPath = resolve(PROJECT_ROOT, 'config/answers.yaml');
const backupDir = mkdtempSync(join(tmpdir(), 'answer-bank-backup-'));

let bank: AnswerBank;
let restoreProfile = false;
let restoreAnswers = false;

before(() => {
  for (const [path, name, example] of [
    [profilePath, 'profile.yaml', 'config/profile.example.yaml'],
    [answersPath, 'answers.yaml', 'config/answers.example.yaml'],
  ] as const) {
    try {
      copyFileSync(path, join(backupDir, name));
      if (name === 'profile.yaml') restoreProfile = true;
      else restoreAnswers = true;
    } catch {
      /* no pre-existing file to preserve */
    }
    copyFileSync(resolve(PROJECT_ROOT, example), path);
  }

  resetAnswerBankCache();
  bank = loadAnswerBank();
});

after(() => {
  if (restoreProfile) copyFileSync(join(backupDir, 'profile.yaml'), profilePath);
  else rmSync(profilePath, { force: true });
  if (restoreAnswers) copyFileSync(join(backupDir, 'answers.yaml'), answersPath);
  else rmSync(answersPath, { force: true });
  rmSync(backupDir, { recursive: true, force: true });
  resetAnswerBankCache();
});

test('the shipped example files are valid', () => {
  assert.equal(bank.profile.identity.firstName, 'Ada');
  assert.ok(bank.answers.custom.length > 0);
});

test('answers identity and contact questions from the profile', () => {
  assert.equal(lookupAnswer('First name', bank), 'Ada');
  assert.equal(lookupAnswer('Last name *', bank), 'Lovelace');
  assert.equal(lookupAnswer('Email address', bank), 'ada@example.com');
  assert.equal(lookupAnswer('Phone number', bank), '+1 555 010 0100');
  assert.equal(lookupAnswer('LinkedIn Profile URL', bank), 'https://www.linkedin.com/in/example');
  assert.equal(lookupAnswer('City', bank), 'San Francisco');
  assert.equal(lookupAnswer('Zip / Postal Code', bank), '94105');
});

test('answers work-authorization questions in their common phrasings', () => {
  assert.equal(lookupAnswer('Are you legally authorized to work in the United States?', bank), 'Yes');
  assert.equal(lookupAnswer('Do you now or in the future require sponsorship?', bank), 'No');
  assert.equal(lookupAnswer('Will you require visa sponsorship?', bank), 'No');
});

test('distinguishes expected salary from current salary', () => {
  assert.equal(lookupAnswer('What is your expected salary?', bank), '180000');
  assert.equal(lookupAnswer('Current annual salary', bank), 'Prefer not to disclose');
});

test('answers EEO questions with the configured non-disclosure', () => {
  assert.equal(lookupAnswer('Gender', bank), 'Decline to self-identify');
  assert.equal(lookupAnswer('Are you Hispanic or Latino?', bank), 'Decline to self-identify');
  assert.equal(lookupAnswer('Veteran status', bank), "I don't wish to answer");
  assert.equal(lookupAnswer('Disability status', bank), "I don't wish to answer");
});

test('prefers a specific skill over the generic experience answer', () => {
  assert.equal(lookupAnswer('How many years of experience do you have with Kubernetes?', bank), '3');
  assert.equal(lookupAnswer('Years of professional experience', bank), '6');
});

test('falls back to custom regex entries', () => {
  assert.equal(lookupAnswer('What is your notice period?', bank), '2 weeks');
  assert.equal(lookupAnswer('Are you at least 18 years of age?', bank), 'Yes');
  assert.match(lookupAnswer('Why do you want to work at Example Corp?', bank) ?? '', /infrastructure/);
});

test('returns null for an unknown question so the caller can escalate', () => {
  assert.equal(
    lookupAnswer('Describe a time you disagreed with your manager about database indexing', bank),
    null,
  );
  assert.equal(lookupAnswer('', bank), null);
});

test('does not mistake unrelated wording for a known question', () => {
  // "source" alone used to match the referral question; a skills field must not.
  assert.equal(lookupAnswer('List your open source contributions', bank), null);
});

test('rejects an invalid regex in the answer bank instead of failing mid-application', () => {
  writeFileSync(answersPath, 'custom:\n  - match: "unclosed ("\n    answer: "x"\n');
  resetAnswerBankCache();
  assert.throws(() => loadAnswerBank(), /not a valid regular expression/);

  copyFileSync(resolve(PROJECT_ROOT, 'config/answers.example.yaml'), answersPath);
  resetAnswerBankCache();
  bank = loadAnswerBank();
});

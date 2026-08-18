import assert from 'node:assert/strict';
import { test } from 'node:test';
import { pickVerificationCode, pickVerificationLink } from './webmail.ts';

process.env.OPENAI_API_KEY ??= 'test-key';
process.env.APPLICATION_EMAIL ??= 'ada@example.com';

test('picks a verification link out of a message body', () => {
  const body = `
    Welcome to Acme Careers.
    Please confirm your address: https://careers.acme.com/verify?token=abc123
    If you did not sign up, ignore this email.
  `;
  assert.equal(pickVerificationLink([body]), 'https://careers.acme.com/verify?token=abc123');
});

test('prefers a real href over the same URL rendered as text', () => {
  const hrefs = ['https://acme.com/activate?token=xyz'];
  assert.equal(pickVerificationLink([...hrefs, 'click here to confirm']), 'https://acme.com/activate?token=xyz');
});

test('strips trailing punctuation that would break the URL', () => {
  const body = 'Confirm at https://acme.com/verify?t=1 (expires soon).';
  assert.equal(pickVerificationLink([body]), 'https://acme.com/verify?t=1');
});

test('recognises a token link even without a verify-like path', () => {
  const body = 'Finish signing up: https://acme.com/u/s?activation=9f8e7d';
  assert.equal(pickVerificationLink([body]), 'https://acme.com/u/s?activation=9f8e7d');
});

test('returns null when there is no verification link', () => {
  const body = 'Thanks for your interest. Browse more jobs at https://acme.com/careers';
  assert.equal(pickVerificationLink([body]), null);
});

test('extracts an explicitly labelled code', () => {
  assert.equal(pickVerificationCode('Your verification code is 481920. It expires in 10 minutes.'), '481920');
  assert.equal(pickVerificationCode('PIN: 3344'), '3344');
});

test('extracts a bare six-digit code', () => {
  assert.equal(pickVerificationCode('Enter 902133 to continue.'), '902133');
});

test('prefers the labelled code over an unrelated number', () => {
  // Year-like and amount-like numbers appear in mail footers; the label wins.
  const body = 'Copyright 2026 Acme. Your code is 774411 and expires shortly.';
  assert.equal(pickVerificationCode(body), '774411');
});

test('returns null when there is no code', () => {
  assert.equal(pickVerificationCode('Please click the button below to continue.'), null);
});

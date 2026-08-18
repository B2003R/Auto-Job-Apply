import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { chromium, type Browser, type Page } from 'playwright';
import {
  countFilledFields,
  extractFormSnapshot,
  unfilledRequiredFields,
  type FormSnapshot,
} from './formExtractor.ts';

const html = readFileSync(join(import.meta.dirname, 'fixtures/ats-form.html'), 'utf8');

let browser: Browser;
let page: Page;
let snapshot: FormSnapshot;

before(async () => {
  browser = await chromium.launch();
  page = await browser.newPage();
  await page.setContent(html);
  snapshot = await extractFormSnapshot(page);
});

after(async () => {
  await browser?.close();
});

const byLabel = (fragment: string) =>
  snapshot.fields.find((f) => f.label.toLowerCase().includes(fragment.toLowerCase()));

test('reads the page title and headings', () => {
  assert.match(snapshot.title, /Senior Engineer/);
  assert.ok(snapshot.headings.some((h) => /Apply for Senior Engineer/.test(h)));
});

test('resolves labels from label[for], aria-label, wrapping label and aria-labelledby', () => {
  assert.ok(byLabel('First name'), 'label[for] not resolved');
  assert.ok(byLabel('Last name'), 'aria-label not resolved');
  assert.ok(byLabel('Email address'), 'wrapping label not resolved');
  assert.ok(byLabel('LinkedIn profile'), 'aria-labelledby not resolved');
});

test('resolves a styled div acting as a label', () => {
  const phone = byLabel('Phone number');
  assert.ok(phone, 'div-based label not resolved');
  assert.equal(phone.kind, 'tel');
});

test('treats a trailing asterisk as required even without the attribute', () => {
  const phone = byLabel('Phone number');
  assert.ok(phone?.required, 'asterisk-only required marker missed');
});

test('captures existing values and leaves empty fields empty', () => {
  assert.equal(byLabel('First name')?.value, 'Ada');
  assert.equal(byLabel('Email address')?.value, 'ada@example.com');
  assert.equal(byLabel('Last name')?.value, '');
});

test('extracts select options', () => {
  const auth = byLabel('authorized to work');
  assert.equal(auth?.kind, 'select');
  assert.deepEqual(auth?.options, ['Yes', 'No']);
  assert.ok(auth?.required);
});

test('collapses a radio group into one field carrying its options and selection', () => {
  const radios = snapshot.fields.filter((f) => f.kind === 'radio');
  assert.equal(radios.length, 1, 'radio group should collapse to a single field');
  const sponsorship = radios[0];
  assert.deepEqual(sponsorship?.options, ['Yes', 'No']);
  assert.equal(sponsorship?.value, 'No', 'checked radio should be reported as the value');
});

test('associates an inline validation error with its field', () => {
  const years = byLabel('Years of experience');
  assert.match(years?.error ?? '', /whole number/);
});

test('collects page-level error banners', () => {
  assert.ok(snapshot.errorBanners.some((b) => /correct the errors/i.test(b)));
});

test('ignores hidden inputs and display:none controls', () => {
  assert.equal(
    snapshot.fields.some((f) => f.value === 'ignored'),
    false,
    'display:none control should be skipped',
  );
  assert.equal(
    snapshot.fields.some((f) => f.value === 'tok'),
    false,
    'hidden input should be skipped',
  );
});

test('classifies buttons into submit, next and back', () => {
  const kinds = new Map(snapshot.buttons.map((b) => [b.label.trim(), b.kind]));
  assert.equal(kinds.get('Submit Application'), 'submit');
  assert.equal(kinds.get('Save and Continue'), 'next');
  assert.equal(kinds.get('Back'), 'back');
});

test('reports required fields that are still empty', () => {
  const outstanding = unfilledRequiredFields(snapshot).map((f) => f.label);
  assert.ok(outstanding.some((l) => /Last name/.test(l)));
  assert.ok(outstanding.some((l) => /authorized to work/.test(l)));
  assert.ok(outstanding.some((l) => /terms/i.test(l)), 'unchecked required checkbox should count as unfilled');
  assert.equal(
    outstanding.some((l) => /First name/.test(l)),
    false,
    'already-filled required field must not be reported',
  );
});

test('counts filled controls, treating checked boxes as filled', async () => {
  const before = await countFilledFields(page);
  await page.locator('#terms').check();
  const afterCheck = await countFilledFields(page);
  assert.equal(afterCheck, before + 1);
});

test('stamps stable handles that resolve back to the element', async () => {
  const first = byLabel('First name');
  assert.ok(first);
  const value = await page.locator(`[data-agent-field="${first.id}"]`).inputValue();
  assert.equal(value, 'Ada');
});

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { chromium, type Browser, type Page } from 'playwright';

process.env.OPENAI_API_KEY ??= 'test-key';
process.env.APPLICATION_EMAIL ??= 'test@example.com';

const { runJobrightAutofill, loadJobrightSelectors } = await import('./autofill.ts');

const simHtml = readFileSync(join(import.meta.dirname, '../apply/fixtures/jobright-sim.html'), 'utf8');
const plainHtml = readFileSync(join(import.meta.dirname, '../apply/fixtures/ats-form.html'), 'utf8');

let browser: Browser;

before(async () => {
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
});

async function pageWith(html: string, query = ''): Promise<Page> {
  const page = await browser.newPage();
  // A real URL is needed so location.search works in the fixture.
  await page.route('**/app*', (route) => route.fulfill({ contentType: 'text/html', body: html }));
  await page.goto(`https://ats.example.com/app${query}`);
  return page;
}

test('selector config parses', () => {
  const selectors = loadJobrightSelectors();
  assert.ok(selectors.trigger.accessibleNames.length > 0);
  assert.ok(selectors.progressPatterns.length > 0);
});

test('finds the trigger, waits for the fill to finish, and reads the progress panel', async () => {
  const page = await pageWith(simHtml);
  try {
    const result = await runJobrightAutofill(page);

    assert.equal(result.triggered, true, 'trigger should be found');
    assert.equal(result.progressSource, 'panel', 'progress should come from the panel');
    assert.equal(result.filled, 4);
    assert.equal(result.required, 4);
    assert.equal(result.complete, true);
    assert.equal(result.fieldsWithValues, 4, 'should wait for every field, not return early');
    assert.equal(result.blockedReason, null);
  } finally {
    await page.close();
  }
});

test('reports incomplete and surfaces the reason when the extension gives up midway', async () => {
  const page = await pageWith(simHtml, '?stallAt=2');
  try {
    const result = await runJobrightAutofill(page);

    assert.equal(result.triggered, true);
    assert.equal(result.filled, 2);
    assert.equal(result.required, 4);
    assert.equal(result.complete, false, 'a partial fill must not be reported complete');
    assert.match(result.blockedReason ?? '', /unsupported form/i);
  } finally {
    await page.close();
  }
});

test('degrades gracefully when no trigger exists rather than throwing', async () => {
  const page = await pageWith(plainHtml);
  try {
    const result = await runJobrightAutofill(page);

    assert.equal(result.triggered, false);
    assert.equal(result.complete, false);
    assert.equal(result.progressSource, 'none');
    // The DOM count is still reported so the caller knows the form is not blank.
    assert.ok(result.fieldsWithValues > 0, 'pre-filled fields should still be counted');
  } finally {
    await page.close();
  }
});

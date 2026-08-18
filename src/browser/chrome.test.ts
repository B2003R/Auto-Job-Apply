import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';

// A real Chrome launch is the whole point of this harness, so these tests spawn
// one. They need a display; run under xvfb-run on a headless machine.
const profileDir = mkdtempSync(join(tmpdir(), 'auto-apply-profile-'));

process.env.CHROME_PROFILE_DIR = profileDir;
process.env.CHROME_DEBUG_PORT = String(19_000 + Math.floor(Math.random() * 1000));
process.env.OPENAI_API_KEY = 'test-key';
process.env.APPLICATION_EMAIL = 'test@example.com';

const { launchBrowser, resolveChromePath } = await import('./chrome.ts');
const { detectJobrightExtension } = await import('../jobright/extension.ts');

let session: Awaited<ReturnType<typeof launchBrowser>> | null = null;

before(async () => {
  session = await launchBrowser();
});

after(async () => {
  await session?.close();
  rmSync(profileDir, { recursive: true, force: true });
});

test('resolves a Chrome binary on this platform', () => {
  assert.ok(resolveChromePath().length > 0);
});

test('attaches over CDP and exposes a browser context', () => {
  assert.ok(session, 'session should be created');
  assert.ok(session.context, 'context should exist');
});

test('drives a page in the attached profile', async () => {
  assert.ok(session);
  const page = await session.newPage();
  try {
    await page.setContent('<h1 id="probe">attached</h1>');
    assert.equal(await page.locator('#probe').innerText(), 'attached');
  } finally {
    await page.close();
  }
});

test('reports the Jobright extension as absent in a clean profile', async () => {
  assert.ok(session);
  const info = await detectJobrightExtension(session.context);
  assert.equal(info.installed, false);
});

test('a second launch attaches to the running Chrome instead of failing', async () => {
  const second = await launchBrowser();
  try {
    assert.ok(second.context);
  } finally {
    // Detach only: closing this must not kill the browser the suite still uses.
    await second.browser.close();
  }
  assert.ok(session);
  const page = await session.newPage();
  await page.close();
});

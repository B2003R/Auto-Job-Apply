import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { Frame, Locator, Page } from 'playwright';
import { parse as parseYaml } from 'yaml';
import { z } from 'zod';
import { PROJECT_ROOT, loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { pause, sleep } from '../browser/humanize.ts';
import { countFilledFields } from '../apply/formExtractor.ts';

const selectorsSchema = z.object({
  trigger: z.object({
    accessibleNames: z.array(z.string()),
    css: z.array(z.string()),
  }),
  panel: z.object({ css: z.array(z.string()) }),
  progressPatterns: z.array(z.string()),
  blockedPatterns: z.array(z.string()),
});

export type JobrightSelectors = z.infer<typeof selectorsSchema>;

let selectorsCache: JobrightSelectors | null = null;

export function loadJobrightSelectors(): JobrightSelectors {
  if (selectorsCache) return selectorsCache;
  const path = resolve(PROJECT_ROOT, 'config/jobright-selectors.yaml');
  const parsed = selectorsSchema.safeParse(parseYaml(readFileSync(path, 'utf8')));
  if (!parsed.success) {
    throw new Error(`Invalid config/jobright-selectors.yaml: ${parsed.error.message}`);
  }
  selectorsCache = parsed.data;
  return selectorsCache;
}

/** Where the filled/required numbers came from, so callers can weigh them. */
export type ProgressSource = 'panel' | 'field-count' | 'none';

export interface AutofillResult {
  /** True when the extension's trigger was found and clicked. */
  triggered: boolean;
  /** Required fields the extension reports as filled, when it reports at all. */
  filled: number | null;
  required: number | null;
  progressSource: ProgressSource;
  /** Non-empty form controls counted directly from the DOM, always available. */
  fieldsWithValues: number;
  /** Extension-surfaced reason it declined to fill, if any. */
  blockedReason: string | null;
  /** True only when the panel explicitly reports every required field filled. */
  complete: boolean;
}

/**
 * Trigger Jobright autofill on the current page and report what it achieved.
 *
 * A missing trigger is not a failure: the caller's LLM repair pass can fill the
 * form from the profile and answer bank on its own, just more slowly. So this
 * returns a result describing what happened rather than throwing.
 */
export async function runJobrightAutofill(page: Page): Promise<AutofillResult> {
  const selectors = loadJobrightSelectors();
  const { timeouts } = loadConfig({ requireEnv: false });

  const before = await countFilledFields(page);
  const trigger = await findTrigger(page, selectors);

  if (!trigger) {
    logger.warn('Jobright autofill trigger not found; falling back to LLM-driven fill', { url: page.url() });
    return {
      triggered: false,
      filled: null,
      required: null,
      progressSource: 'none',
      fieldsWithValues: before,
      blockedReason: null,
      complete: false,
    };
  }

  await trigger.click({ timeout: 5000 }).catch(async () => {
    // Extension overlays sometimes intercept pointer events; a forced click is
    // acceptable here because the target is already a resolved visible element.
    await trigger.click({ force: true, timeout: 5000 });
  });
  logger.info('Clicked Jobright autofill');
  await pause(600, 1200);

  const settled = await waitForAutofillToSettle(page, selectors, timeouts.autofillSettle);
  const blockedReason = await readBlockedReason(page, selectors);

  const result: AutofillResult = {
    triggered: true,
    filled: settled.filled,
    required: settled.required,
    progressSource: settled.source,
    fieldsWithValues: settled.fieldsWithValues,
    blockedReason,
    complete: settled.filled !== null && settled.required !== null && settled.filled >= settled.required,
  };

  logger.info('Autofill finished', {
    filled: result.filled,
    required: result.required,
    source: result.progressSource,
    fieldsWithValues: result.fieldsWithValues,
    gained: result.fieldsWithValues - before,
    blockedReason: result.blockedReason,
  });

  return result;
}

/**
 * Poll until the fill stops changing.
 *
 * Two independent signals are tracked because either may be unavailable: the
 * extension's own progress readout, and a direct count of non-empty controls.
 * Settling on the DOM count means this still terminates sensibly when the panel
 * cannot be found or its wording changes.
 */
async function waitForAutofillToSettle(
  page: Page,
  selectors: JobrightSelectors,
  timeoutMs: number,
): Promise<{ filled: number | null; required: number | null; source: ProgressSource; fieldsWithValues: number }> {
  const deadline = Date.now() + timeoutMs;
  const pollMs = 700;
  const stableThreshold = 3;

  let stable = 0;
  let lastSignature = '';
  let progress: { filled: number; required: number } | null = null;
  let fieldsWithValues = 0;

  while (Date.now() < deadline) {
    progress = await readProgress(page, selectors);
    fieldsWithValues = await countFilledFields(page);

    const signature = `${progress?.filled ?? '-'}:${progress?.required ?? '-'}:${fieldsWithValues}`;
    if (signature === lastSignature) {
      stable += 1;
      if (stable >= stableThreshold) break;
    } else {
      stable = 0;
      lastSignature = signature;
    }

    // A complete panel readout is a definitive stop; no need to keep polling.
    if (progress && progress.filled >= progress.required && progress.required > 0) break;

    await sleep(pollMs);
  }

  return {
    filled: progress?.filled ?? null,
    required: progress?.required ?? null,
    source: progress ? 'panel' : 'field-count',
    fieldsWithValues,
  };
}

async function readProgress(
  page: Page,
  selectors: JobrightSelectors,
): Promise<{ filled: number; required: number } | null> {
  const patterns = selectors.progressPatterns.map((p) => new RegExp(p, 'i'));

  for (const text of await collectPanelTexts(page, selectors)) {
    for (const pattern of patterns) {
      const match = pattern.exec(text);
      if (!match?.[1] || !match[2]) continue;
      const filled = Number(match[1]);
      const required = Number(match[2]);
      if (Number.isFinite(filled) && Number.isFinite(required) && required > 0) {
        return { filled, required };
      }
    }
  }
  return null;
}

/**
 * Gather candidate text to scan for the progress readout. Falls back to whole-page
 * text because the panel selector is the most likely part of this to go stale, and
 * the progress regexes are specific enough to survive the wider search.
 */
async function collectPanelTexts(page: Page, selectors: JobrightSelectors): Promise<string[]> {
  const texts: string[] = [];

  for (const frame of relevantFrames(page)) {
    for (const css of selectors.panel.css) {
      const locator = frame.locator(css).first();
      // isVisible resolves immediately for absent elements, whereas innerText
      // auto-waits. Gating on it keeps a miss from costing a timeout, which
      // matters because this runs on every settle poll.
      if (!(await locator.isVisible().catch(() => false))) continue;
      const text = await locator.innerText({ timeout: 800 }).catch(() => null);
      if (text) {
        texts.push(text);
        break;
      }
    }
  }

  if (texts.length === 0) {
    const body = await page.locator('body').innerText({ timeout: 1500 }).catch(() => null);
    if (body) texts.push(body);
  }
  return texts;
}

async function readBlockedReason(page: Page, selectors: JobrightSelectors): Promise<string | null> {
  const patterns = selectors.blockedPatterns.map((p) => new RegExp(p, 'i'));
  for (const text of await collectPanelTexts(page, selectors)) {
    for (const pattern of patterns) {
      const match = pattern.exec(text);
      if (match) return match[0];
    }
  }
  return null;
}

/**
 * Locate the autofill trigger, trying accessible names before raw CSS so the
 * match is driven by what the button says rather than by markup details.
 */
async function findTrigger(page: Page, selectors: JobrightSelectors): Promise<Locator | null> {
  for (const frame of relevantFrames(page)) {
    for (const name of selectors.trigger.accessibleNames) {
      const pattern = new RegExp(name, 'i');
      for (const role of ['button', 'link'] as const) {
        const locator = frame.getByRole(role, { name: pattern }).first();
        if (await isUsable(locator)) return locator;
      }
    }

    for (const css of selectors.trigger.css) {
      const locator = frame.locator(css).first();
      if (await isUsable(locator)) return locator;
    }
  }
  return null;
}

/**
 * Frames worth searching: the main frame plus extension frames. Cross-origin ATS
 * iframes are included because some vendors render the form itself in one.
 */
function relevantFrames(page: Page): Frame[] {
  const frames = page.frames();
  return frames.length > 8 ? [page.mainFrame(), ...frames.slice(0, 8)] : frames;
}

async function isUsable(locator: Locator): Promise<boolean> {
  return locator.isVisible({ timeout: 700 }).catch(() => false);
}

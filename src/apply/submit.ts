import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { Page } from 'playwright';
import { loadConfig, type RunMode } from '../config.ts';
import { logger } from '../logger.ts';
import { pause, waitForDomToSettle } from '../browser/humanize.ts';
import { confirm } from '../ui/prompt.ts';
import { BUTTON_ATTR, extractFormSnapshot, unfilledRequiredFields, type FormSnapshot } from './formExtractor.ts';
import { confirmSubmission } from './pageClassifier.ts';
import type { AutofillResult } from '../jobright/autofill.ts';
import type { Verdict } from '../llm/schemas.ts';

export type SubmitDecision =
  | { submit: true }
  | { submit: false; reason: string; needsHuman: boolean };

export interface GateInput {
  verdictStatus: Verdict['status'];
  autofill: AutofillResult;
  snapshot: FormSnapshot;
  mode: RunMode;
}

/**
 * Decide whether to submit. This is deliberately code, not a model call.
 *
 * Requires agreement from two independent sources before acting: the verifier's
 * judgement, and a mechanical check that no required field is empty. A model can
 * be confidently wrong; the field check cannot, so requiring both means a single
 * bad verdict cannot put an incomplete application into the world.
 */
export function decideSubmission(input: GateInput): SubmitDecision {
  const config = loadConfig();
  const { verdictStatus, autofill, snapshot, mode } = input;

  if (verdictStatus !== 'ready_to_submit') {
    return {
      submit: false,
      reason: `Verifier status was "${verdictStatus}", not ready_to_submit`,
      needsHuman: verdictStatus === 'needs_human',
    };
  }

  const outstanding = unfilledRequiredFields(snapshot);
  if (outstanding.length > 0) {
    return {
      submit: false,
      reason: `${outstanding.length} required field(s) still empty: ${outstanding
        .map((f) => f.label || f.id)
        .slice(0, 5)
        .join(', ')}`,
      needsHuman: true,
    };
  }

  const fieldsWithErrors = snapshot.fields.filter((f) => f.error && !f.disabled);
  if (fieldsWithErrors.length > 0) {
    return {
      submit: false,
      reason: `Validation errors remain: ${fieldsWithErrors.map((f) => `${f.label}: ${f.error}`).slice(0, 3).join('; ')}`,
      needsHuman: true,
    };
  }

  // The extension's own readout is a third opinion when it is available.
  if (
    config.safety.requireAllRequiredFieldsFilled &&
    autofill.required !== null &&
    autofill.filled !== null &&
    autofill.filled < autofill.required
  ) {
    return {
      submit: false,
      reason: `Jobright reports only ${autofill.filled} of ${autofill.required} required fields filled`,
      needsHuman: true,
    };
  }

  if (!snapshot.buttons.some((b) => b.kind === 'submit' && !b.disabled)) {
    return { submit: false, reason: 'No enabled submit control found on the page', needsHuman: true };
  }

  if (mode === 'dry-run') {
    return { submit: false, reason: 'dry-run mode: form was filled and verified but not submitted', needsHuman: false };
  }

  return { submit: true };
}

export interface SubmitResult {
  submitted: boolean;
  confirmed: boolean;
  reason: string;
  screenshotPath: string | null;
  finalUrl: string;
}

/**
 * Click submit and verify it actually went through.
 *
 * A click that appears to work is not evidence of submission, so the result is
 * only reported as confirmed when the resulting page says so. An unconfirmed
 * submission is surfaced rather than assumed successful, because the operator
 * needs to know which ones to check by hand.
 */
export async function submitApplication(
  page: Page,
  options: { mode: RunMode; jobLabel: string; artifactPrefix: string },
): Promise<SubmitResult> {
  const snapshot = await extractFormSnapshot(page);
  const button = snapshot.buttons.find((b) => b.kind === 'submit' && !b.disabled);

  if (!button) {
    return {
      submitted: false,
      confirmed: false,
      reason: 'No enabled submit control found at submission time',
      screenshotPath: null,
      finalUrl: page.url(),
    };
  }

  if (options.mode === 'review') {
    const proceed = await confirm(
      `\n>>> Ready to submit: ${options.jobLabel}\n    ${page.url()}\n    Submit this application?`,
      false,
    );
    if (!proceed) {
      return {
        submitted: false,
        confirmed: false,
        reason: 'Operator declined in review mode',
        screenshotPath: await captureScreenshot(page, `${options.artifactPrefix}-declined`),
        finalUrl: page.url(),
      };
    }
  }

  logger.info('Submitting application', { job: options.jobLabel, button: button.label });
  await pause(400, 1100);

  await page.locator(`[${BUTTON_ATTR}="${button.id}"]`).first().click({ timeout: 15_000 });

  // ATS submissions typically navigate or swap the view; allow for either.
  await page.waitForLoadState('domcontentloaded', { timeout: 30_000 }).catch(() => undefined);
  await waitForDomToSettle(page, 1200, 15_000);

  const screenshotPath = await captureScreenshot(page, `${options.artifactPrefix}-submitted`);
  const confirmation = await confirmSubmission(page);

  return {
    submitted: true,
    confirmed: confirmation.confirmed,
    reason: confirmation.confirmed
      ? confirmation.evidence
      : `Clicked submit but could not confirm receipt (${confirmation.evidence || 'no confirmation text found'})`,
    screenshotPath,
    finalUrl: page.url(),
  };
}

/** Save a screenshot as evidence. Returns null rather than throwing on failure. */
export async function captureScreenshot(page: Page, name: string): Promise<string | null> {
  const { paths } = loadConfig({ requireEnv: false });
  const day = new Date().toISOString().slice(0, 10);
  const dir = join(paths.artifactsDir, day);

  try {
    mkdirSync(dir, { recursive: true });
    const path = join(dir, `${name.replace(/[^a-z0-9_-]+/gi, '-').slice(0, 120)}.png`);
    await page.screenshot({ path, fullPage: true });
    return path;
  } catch (error) {
    logger.debug('Screenshot failed', { error: error instanceof Error ? error.message : String(error) });
    return null;
  }
}

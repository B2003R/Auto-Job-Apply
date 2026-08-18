import { existsSync } from 'node:fs';
import type { Locator, Page } from 'playwright';
import { loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { pause, typeLikeHuman, waitForDomToSettle } from '../browser/humanize.ts';
import { requestStructured } from '../llm/client.ts';
import {
  VERIFIER_SYSTEM_PROMPT,
  buildVerifierUserMessage,
  type VerifierInput,
} from '../llm/prompts.ts';
import { verdictJsonSchema, verdictSchema, type RepairAction, type Verdict } from '../llm/schemas.ts';
import type { AutofillResult } from '../jobright/autofill.ts';
import { loadAnswerBank, lookupAnswer, serialiseBankForPrompt } from './answerBank.ts';
import {
  FIELD_ATTR,
  BUTTON_ATTR,
  extractFormSnapshot,
  unfilledRequiredFields,
  type FormField,
  type FormSnapshot,
} from './formExtractor.ts';

export interface RepairContext {
  company?: string | null;
  title?: string | null;
  autofill: AutofillResult;
  /** Recorded for the operator so the answer bank can be extended. */
  onUnanswerable?(question: string, field: { label: string; kind: string; options?: string[] }): void;
}

export interface RepairOutcome {
  status: Verdict['status'];
  blockers: string[];
  unanswerable: Array<{ question: string; label: string }>;
  loops: number;
  appliedActions: number;
  rejectedActions: string[];
  /** Present when the verifier asked to advance a multi-page form. */
  nextButtonId: string | null;
  finalSnapshot: FormSnapshot;
}

/**
 * Fill required fields that the answer bank can resolve without a model call.
 *
 * Running before the verifier keeps token cost and latency down, and makes the
 * common path deterministic: identical forms get identical values every time.
 */
export async function applyDeterministicAnswers(
  page: Page,
  snapshot: FormSnapshot,
): Promise<{ filled: number; resolved: Array<{ fieldId: string; label: string; answer: string }> }> {
  const bank = loadAnswerBank();
  const resolved: Array<{ fieldId: string; label: string; answer: string }> = [];
  let filled = 0;

  for (const field of unfilledRequiredFields(snapshot)) {
    const answer = lookupAnswer(field.label, bank);
    if (!answer) continue;

    resolved.push({ fieldId: field.id, label: field.label, answer });

    const applied = await applyValueToField(page, field, answer).catch((error: unknown) => {
      logger.debug('Deterministic fill failed', {
        field: field.label,
        error: error instanceof Error ? error.message : String(error),
      });
      return false;
    });
    if (applied) filled += 1;
  }

  if (filled > 0) {
    logger.info('Filled fields from the answer bank', { count: filled });
    await waitForDomToSettle(page, 500, 5000);
  }

  return { filled, resolved };
}

/**
 * Verify the form and repair it, looping until it is ready or the budget runs out.
 *
 * Never submits. The caller decides that, using this outcome plus its own
 * completeness check, which is what keeps a confident-but-wrong model verdict
 * from putting an incomplete application into the world.
 */
export async function verifyAndRepair(page: Page, context: RepairContext): Promise<RepairOutcome> {
  const config = loadConfig();
  const maxLoops = Math.max(1, config.safety.maxRepairLoops);
  const bank = loadAnswerBank();
  const applicantData = serialiseBankForPrompt(bank);
  const resumeAvailable = config.paths.resume !== '' && existsSync(config.paths.resume);

  let snapshot = await extractFormSnapshot(page);
  let appliedActions = 0;
  const rejectedActions: string[] = [];
  const unanswerable: Array<{ question: string; label: string }> = [];
  let verdict: Verdict | null = null;
  let loops = 0;

  const deterministic = await applyDeterministicAnswers(page, snapshot);
  if (deterministic.filled > 0) {
    appliedActions += deterministic.filled;
    snapshot = await extractFormSnapshot(page);
  }

  for (loops = 1; loops <= maxLoops; loops += 1) {
    const screenshot = await page.screenshot({ fullPage: false }).catch(() => null);

    const input: VerifierInput = {
      snapshot,
      autofill: context.autofill,
      applicantData,
      resolvedAnswers: deterministic.resolved,
      jobContext: { company: context.company, title: context.title, url: page.url() },
      attempt: loops,
      maxAttempts: maxLoops,
      resumeAvailable,
    };

    verdict = await requestStructured({
      purpose: 'verify-form',
      system: VERIFIER_SYSTEM_PROMPT,
      user: buildVerifierUserMessage(input),
      screenshotBase64: screenshot ? screenshot.toString('base64') : undefined,
      schemaName: 'form_verdict',
      jsonSchema: verdictJsonSchema as unknown as Record<string, unknown>,
      validator: verdictSchema,
    });

    for (const item of verdict.unanswerable) {
      const field = snapshot.fields.find((f) => f.id === item.fieldId);
      const label = field?.label ?? item.fieldId;
      unanswerable.push({ question: item.question || label, label });
      context.onUnanswerable?.(item.question || label, {
        label,
        kind: field?.kind ?? 'unknown',
        options: field?.options,
      });
    }

    logger.info('Verifier verdict', {
      status: verdict.status,
      loop: loops,
      fixes: verdict.fixes.length,
      blockers: verdict.blockers.length,
      unanswerable: verdict.unanswerable.length,
    });

    if (verdict.status === 'ready_to_submit' || verdict.status === 'needs_human' || verdict.status === 'not_an_application_form') {
      break;
    }

    if (verdict.status === 'needs_next_step') break;

    const { allowed, rejected } = sanitiseFixes(verdict.fixes, snapshot);
    rejectedActions.push(...rejected);

    if (allowed.length === 0) {
      logger.warn('Verifier asked for fixes but none were executable', { rejected: rejected.length });
      break;
    }

    for (const action of allowed) {
      const ok = await executeAction(page, action, snapshot).catch((error: unknown) => {
        logger.warn('Repair action failed', {
          action: action.action,
          error: error instanceof Error ? error.message : String(error),
        });
        return false;
      });
      if (ok) appliedActions += 1;
      await pause(180, 520);
    }

    await waitForDomToSettle(page, 600, 8000);
    snapshot = await extractFormSnapshot(page);
  }

  const nextButtonId =
    verdict?.status === 'needs_next_step'
      ? verdict.fixes.find((f) => f.action === 'click_button')?.buttonId ?? null
      : null;

  return {
    status: verdict?.status ?? 'needs_human',
    blockers: verdict?.blockers ?? ['Verifier produced no verdict'],
    unanswerable,
    loops: Math.min(loops, maxLoops),
    appliedActions,
    rejectedActions,
    nextButtonId,
    finalSnapshot: snapshot,
  };
}

/**
 * Drop any action that does not refer to something in the snapshot we provided.
 *
 * This is the boundary that keeps model output from becoming arbitrary page
 * interaction: unknown ids, options that were never offered, and submit-like
 * buttons are all refused here rather than trusted.
 */
export function sanitiseFixes(
  fixes: RepairAction[],
  snapshot: FormSnapshot,
): { allowed: RepairAction[]; rejected: string[] } {
  const fieldsById = new Map(snapshot.fields.map((f) => [f.id, f]));
  const buttonsById = new Map(snapshot.buttons.map((b) => [b.id, b]));
  const allowed: RepairAction[] = [];
  const rejected: string[] = [];

  for (const fix of fixes) {
    if (fix.action === 'click_button') {
      const button = buttonsById.get(fix.buttonId);
      if (!button) {
        rejected.push(`click_button referenced unknown button ${fix.buttonId}`);
        continue;
      }
      if (button.kind === 'submit') {
        // Submission is a code decision, never a model one.
        rejected.push(`click_button refused: "${button.label}" is a submit control`);
        continue;
      }
      if (button.disabled) {
        rejected.push(`click_button refused: "${button.label}" is disabled`);
        continue;
      }
      allowed.push(fix);
      continue;
    }

    const field = fieldsById.get(fix.fieldId);
    if (!field) {
      rejected.push(`${fix.action} referenced unknown field ${fix.fieldId}`);
      continue;
    }
    if (field.disabled) {
      rejected.push(`${fix.action} refused: field "${field.label}" is disabled`);
      continue;
    }

    if ((fix.action === 'select_option' || fix.action === 'choose_radio') && field.options && field.options.length > 0) {
      const match = field.options.find((o) => o.toLowerCase().trim() === fix.value.toLowerCase().trim());
      if (!match) {
        rejected.push(
          `${fix.action} refused: "${fix.value}" is not an option for "${field.label}" (${field.options.join(', ')})`,
        );
        continue;
      }
      allowed.push({ ...fix, value: match });
      continue;
    }

    allowed.push(fix);
  }

  if (rejected.length > 0) logger.warn('Rejected verifier actions', { rejected });
  return { allowed, rejected };
}

async function executeAction(page: Page, action: RepairAction, snapshot: FormSnapshot): Promise<boolean> {
  if (action.action === 'click_button') {
    const locator = page.locator(`[${BUTTON_ATTR}="${action.buttonId}"]`).first();
    await locator.click({ timeout: 8000 });
    return true;
  }

  const field = snapshot.fields.find((f) => f.id === action.fieldId);
  if (!field) return false;

  switch (action.action) {
    case 'set_text':
      return applyValueToField(page, field, action.value);
    case 'select_option':
      return applyValueToField(page, field, action.value);
    case 'choose_radio':
      return applyValueToField(page, field, action.value);
    case 'set_checkbox':
      return applyValueToField(page, field, action.checked ? 'true' : 'false');
    case 'upload_resume': {
      const { paths } = loadConfig();
      if (!paths.resume || !existsSync(paths.resume)) {
        logger.warn('Resume upload requested but no resume file is configured', { path: paths.resume });
        return false;
      }
      await fieldLocator(page, field).setInputFiles(paths.resume);
      return true;
    }
    default:
      return false;
  }
}

/** Write a value into a field, dispatching on how the control actually behaves. */
async function applyValueToField(page: Page, field: FormField, value: string): Promise<boolean> {
  const locator = fieldLocator(page, field);

  switch (field.kind) {
    case 'checkbox': {
      const shouldCheck = /^(true|yes|1|on)$/i.test(value.trim());
      if (shouldCheck) await locator.check({ timeout: 6000 });
      else await locator.uncheck({ timeout: 6000 });
      return true;
    }

    case 'radio': {
      // Members share a group attribute; pick the one whose label matches.
      const members = page.locator(`[${FIELD_ATTR}-group="${field.id}"]`);
      const count = await members.count();
      for (let i = 0; i < count; i += 1) {
        const member = members.nth(i);
        const label = await labelTextFor(member);
        if (label.toLowerCase().trim() === value.toLowerCase().trim()) {
          await member.check({ timeout: 6000 }).catch(async () => {
            await member.click({ force: true, timeout: 6000 });
          });
          return true;
        }
      }
      return false;
    }

    case 'select': {
      const tag = await locator.evaluate((el) => el.tagName.toLowerCase()).catch(() => '');
      if (tag === 'select') {
        await locator.selectOption({ label: value }, { timeout: 6000 });
        return true;
      }
      // Custom combobox: open it, type to filter, then click the matching option.
      await locator.click({ timeout: 6000 });
      await pause(150, 400);
      await page.keyboard.type(value, { delay: 30 });
      await pause(250, 600);
      const option = page
        .getByRole('option', { name: new RegExp(`^\\s*${escapeRegex(value)}\\s*$`, 'i') })
        .first();
      if (await option.isVisible().catch(() => false)) {
        await option.click({ timeout: 6000 });
        return true;
      }
      await page.keyboard.press('Enter');
      return true;
    }

    case 'file': {
      const { paths } = loadConfig();
      if (!paths.resume || !existsSync(paths.resume)) return false;
      await locator.setInputFiles(paths.resume);
      return true;
    }

    case 'textarea':
    case 'text':
    case 'email':
    case 'tel':
    case 'url':
    case 'number':
    case 'date':
      await typeLikeHuman(locator, value);
      return true;

    default:
      // Unknown widget: a plain fill is the best available attempt.
      await locator.fill(value, { timeout: 6000 }).catch(() => undefined);
      return true;
  }
}

function fieldLocator(page: Page, field: FormField): Locator {
  return page.locator(`[${FIELD_ATTR}="${field.id}"]`).first();
}

async function labelTextFor(locator: Locator): Promise<string> {
  return locator
    .evaluate((el) => {
      const input = el as HTMLInputElement;
      if (input.id) {
        const explicit = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
        if (explicit?.textContent) return explicit.textContent.trim();
      }
      const wrapping = input.closest('label');
      if (wrapping?.textContent) return wrapping.textContent.trim();
      return input.value ?? '';
    })
    .catch(() => '');
}

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

import type { Page } from 'playwright';
import { loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { waitForEnterWithTimeout } from '../ui/prompt.ts';
import { classifyPage } from './pageClassifier.ts';

export interface EscalationRequest {
  reason: string;
  jobLabel: string;
  url: string;
  /** When false, notify but do not block; used in unattended runs. */
  interactive: boolean;
}

export interface EscalationResult {
  resolved: boolean;
  detail: string;
}

/**
 * Hand a page to the operator and wait, bounded by safety.humanWaitSeconds.
 *
 * The timeout matters more than it looks: without it one CAPTCHA at 2am stalls
 * the entire day's quota. On timeout the job is abandoned and its URL exported,
 * which is recoverable, whereas a stalled run is not.
 */
export async function escalateToHuman(page: Page, request: EscalationRequest): Promise<EscalationResult> {
  const config = loadConfig();
  const waitMs = config.safety.humanWaitSeconds * 1000;

  logger.warn('Human attention needed', { reason: request.reason, job: request.jobLabel, url: request.url });
  await notify(request);

  if (!request.interactive || waitMs === 0) {
    return { resolved: false, detail: `Unattended run: ${request.reason}` };
  }

  // Bring the tab forward so the operator sees the right thing immediately.
  await page.bringToFront().catch(() => undefined);

  const prompt = [
    '',
    `>>> ${request.reason}`,
    `    ${request.jobLabel}`,
    `    ${request.url}`,
    `    Resolve it in the browser, then press Enter (or wait ${config.safety.humanWaitSeconds}s to skip). `,
  ].join('\n');

  const answered = await waitForEnterWithTimeout(prompt, waitMs);
  if (!answered) {
    logger.warn('Human did not respond in time; skipping job', { url: request.url });
    return { resolved: false, detail: `Timed out after ${config.safety.humanWaitSeconds}s waiting for a human` };
  }

  // Re-read the page rather than trusting that the operator fixed the problem.
  const assessment = await classifyPage(page).catch(() => null);
  if (assessment && assessment.kind === 'captcha') {
    return { resolved: false, detail: 'Challenge still present after human intervention' };
  }

  return { resolved: true, detail: `Human resolved: ${assessment?.kind ?? 'unknown page state'}` };
}

/** Best-effort webhook notification. Never allowed to break a run. */
async function notify(request: EscalationRequest): Promise<void> {
  const { env } = loadConfig();
  if (!env.NOTIFY_WEBHOOK) return;

  const text = `Job application needs attention: ${request.reason}\n${request.jobLabel}\n${request.url}`;
  try {
    await fetch(env.NOTIFY_WEBHOOK, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      // `text` and `content` cover Slack and Discord respectively.
      body: JSON.stringify({ text, content: text }),
      signal: AbortSignal.timeout(5000),
    });
  } catch (error) {
    logger.debug('Notification webhook failed', {
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

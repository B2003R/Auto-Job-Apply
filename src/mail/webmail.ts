import type { BrowserContext, Page } from 'playwright';
import { loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { sleep } from '../browser/humanize.ts';

export interface VerificationHit {
  /** A clickable verification link, when the message contains one. */
  link: string | null;
  /** A numeric code, when the message uses one instead of a link. */
  code: string | null;
  subject: string;
  bodyExcerpt: string;
}

export interface WebmailSearch {
  /** Sender domain to scope the search to, for example "greenhouse.io". */
  senderDomain: string;
  /** Only consider mail newer than this. Set at signup time. */
  since: Date;
  timeoutMs?: number;
}

interface WebmailProvider {
  /** Deep link to a search restricted to recent mail from the sender. */
  searchUrl(senderDomain: string): string;
  /** Selector for a row in the result list. */
  messageRowSelector: string;
  /** Selector for the opened message body. */
  messageBodySelector: string;
}

const PROVIDERS: Record<'gmail' | 'outlook', WebmailProvider> = {
  gmail: {
    // newer_than:1h keeps an old verification mail from being mistaken for this one.
    searchUrl: (domain) =>
      `https://mail.google.com/mail/u/0/#search/${encodeURIComponent(`from:${domain} newer_than:1h`)}`,
    messageRowSelector: 'tr.zA',
    messageBodySelector: 'div.a3s',
  },
  outlook: {
    searchUrl: (domain) => `https://outlook.live.com/mail/0/search/id/${encodeURIComponent(domain)}`,
    messageRowSelector: '[role="option"]',
    messageBodySelector: '[role="document"]',
  },
};

const VERIFICATION_LINK_PATTERNS = [
  /https?:\/\/[^\s"'<>]*(?:verify|confirm|activate|validation|verification)[^\s"'<>]*/i,
  /https?:\/\/[^\s"'<>]*(?:token|activation)=[^\s"'<>]+/i,
];

const CODE_PATTERNS = [
  /\b(?:code|pin|otp)\b[^\d]{0,24}(\d{4,8})\b/i,
  /\b(\d{6})\b/,
  /\b(\d{4})\b(?=[^\d]*(?:is your|to verify|verification))/i,
];

/**
 * Find an ATS verification message by reading the operator's already-signed-in
 * webmail in a browser tab.
 *
 * Deliberately narrow in what it touches: it searches only for recent mail from
 * the specific sender domain, opens only the newest match, and never deletes,
 * archives, or sends anything. The mailbox is the operator's real one, so
 * anything broader would be an overreach.
 */
export async function findVerificationMessage(
  context: BrowserContext,
  search: WebmailSearch,
): Promise<VerificationHit | null> {
  const config = loadConfig();
  const provider = PROVIDERS[config.env.WEBMAIL_PROVIDER];
  const timeoutMs = search.timeoutMs ?? config.timeouts.emailVerification;
  const deadline = Date.now() + timeoutMs;

  // A dedicated tab so the application tab is never navigated away mid-form.
  const page = await context.newPage();

  try {
    let attempt = 0;
    while (Date.now() < deadline) {
      attempt += 1;
      logger.debug('Checking webmail for verification message', {
        senderDomain: search.senderDomain,
        attempt,
      });

      const hit = await checkOnce(page, provider, search);
      if (hit) {
        logger.info('Found verification message', { subject: hit.subject, hasLink: hit.link !== null, hasCode: hit.code !== null });
        return hit;
      }

      // Mail delivery is the slow part; polling faster does not make it arrive.
      await sleep(8000);
    }

    logger.warn('No verification message arrived in time', {
      senderDomain: search.senderDomain,
      waitedMs: timeoutMs,
    });
    return null;
  } finally {
    await page.close().catch(() => undefined);
  }
}

async function checkOnce(
  page: Page,
  provider: WebmailProvider,
  search: WebmailSearch,
): Promise<VerificationHit | null> {
  await page.goto(provider.searchUrl(search.senderDomain), { waitUntil: 'domcontentloaded' }).catch(() => undefined);
  // Webmail clients render the result list asynchronously after navigation.
  await sleep(2500);

  const rows = page.locator(provider.messageRowSelector);
  const count = await rows.count().catch(() => 0);
  if (count === 0) return null;

  // Newest first in both providers' default ordering.
  const newest = rows.first();
  await newest.click({ timeout: 8000 }).catch(() => undefined);
  await sleep(1800);

  const subject = await page
    .locator('h2, [data-legacy-thread-id] h2, [role="heading"]')
    .first()
    .innerText({ timeout: 4000 })
    .catch(() => '');

  const body = await page
    .locator(provider.messageBodySelector)
    .first()
    .innerText({ timeout: 6000 })
    .catch(() => '');

  if (!body) return null;

  // Links are read from href attributes as well as text, because webmail often
  // renders a display label rather than the raw URL.
  const hrefs = await page
    .locator(`${provider.messageBodySelector} a[href]`)
    .evaluateAll((els) => els.map((el) => (el as HTMLAnchorElement).href))
    .catch(() => [] as string[]);

  const link = pickVerificationLink([...hrefs, body]);
  const code = pickVerificationCode(body);

  if (!link && !code) return null;

  return { link, code, subject, bodyExcerpt: body.slice(0, 500) };
}

export function pickVerificationLink(candidates: string[]): string | null {
  for (const pattern of VERIFICATION_LINK_PATTERNS) {
    for (const candidate of candidates) {
      const match = pattern.exec(candidate);
      if (match?.[0]) return stripTrailingPunctuation(match[0]);
    }
  }
  return null;
}

export function pickVerificationCode(body: string): string | null {
  for (const pattern of CODE_PATTERNS) {
    const match = pattern.exec(body);
    if (match?.[1]) return match[1];
  }
  return null;
}

/** Open a verification link in its own tab, leaving the application tab intact. */
export async function completeVerificationLink(context: BrowserContext, link: string): Promise<boolean> {
  const page = await context.newPage();
  try {
    const response = await page.goto(link, { waitUntil: 'domcontentloaded' });
    const ok = response === null || response.ok();
    logger.info('Opened verification link', { ok, status: response?.status() });
    await sleep(1500);
    return ok;
  } catch (error) {
    logger.warn('Verification link failed to open', {
      error: error instanceof Error ? error.message : String(error),
    });
    return false;
  } finally {
    await page.close().catch(() => undefined);
  }
}

function stripTrailingPunctuation(url: string): string {
  return url.replace(/[).,;:'"\]]+$/, '');
}

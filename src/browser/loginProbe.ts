import type { BrowserContext, Page } from 'playwright';
import { waitForDomToSettle } from './humanize.ts';

export type LoginState = 'logged-in' | 'logged-out' | 'unknown';

export interface SiteProbe {
  key: string;
  label: string;
  /** Page to open when checking, and where the operator lands to log in. */
  url: string;
  /** URL substrings that only appear once authenticated. */
  signedInUrlHints?: string[];
  /** Selectors that only exist once authenticated. */
  signedInSelectors?: string[];
  /** URL substrings that indicate a redirect to a sign-in wall. */
  signedOutUrlHints?: string[];
  required: boolean;
  note?: string;
}

export const SITE_PROBES: SiteProbe[] = [
  {
    key: 'jobright',
    label: 'Jobright',
    url: 'https://jobright.ai/jobs/recommend',
    signedInUrlHints: ['/jobs/recommend', '/jobs'],
    signedOutUrlHints: ['/login', '/signin', '/landing'],
    required: true,
    note: 'Source of daily recommendations and the account backing the autofill extension.',
  },
  {
    key: 'webmail',
    label: 'Webmail',
    url: 'https://mail.google.com/mail/u/0/#inbox',
    signedInUrlHints: ['/mail/u/'],
    signedOutUrlHints: ['accounts.google.com', 'ServiceLogin'],
    required: true,
    note: 'Used to read ATS verification emails. Must be the same address as APPLICATION_EMAIL.',
  },
  {
    key: 'linkedin',
    label: 'LinkedIn',
    url: 'https://www.linkedin.com/feed/',
    signedInUrlHints: ['/feed'],
    signedOutUrlHints: ['/login', '/uas/login', '/authwall', 'linkedin.com/?'],
    required: false,
  },
  {
    key: 'wellfound',
    label: 'Wellfound',
    url: 'https://wellfound.com/jobs',
    signedInSelectors: ['a[href*="/logout"]', '[data-test="AvatarMenu"]', 'button[aria-label*="account" i]'],
    signedOutUrlHints: ['/login', '/signup'],
    required: false,
  },
  {
    key: 'handshake',
    label: 'Handshake',
    url: 'https://app.joinhandshake.com/stu/postings',
    signedInUrlHints: ['/stu/'],
    signedOutUrlHints: ['/login', '/explore', 'joinhandshake.com/login'],
    required: false,
    note: 'University SSO gated; Jobright autofill may not support it, so expect more human handoffs.',
  },
];

/**
 * Determine whether the profile is authenticated for a site.
 *
 * Returns 'unknown' rather than guessing when signals conflict: a false
 * 'logged-in' would let a run start and burn quota on sign-in walls, and a false
 * 'logged-out' would nag the operator to re-authenticate needlessly.
 */
export async function probeLogin(context: BrowserContext, probe: SiteProbe): Promise<LoginState> {
  const page = await context.newPage();
  try {
    await page.goto(probe.url, { waitUntil: 'domcontentloaded' }).catch(() => undefined);
    await waitForDomToSettle(page, 600, 8000);

    const finalUrl = page.url();

    if (probe.signedOutUrlHints?.some((hint) => finalUrl.includes(hint))) return 'logged-out';

    if (probe.signedInSelectors) {
      for (const selector of probe.signedInSelectors) {
        if (await page.locator(selector).first().isVisible({ timeout: 1500 }).catch(() => false)) {
          return 'logged-in';
        }
      }
    }

    if (probe.signedInUrlHints?.some((hint) => finalUrl.includes(hint))) {
      if (await hasSignInAffordance(page)) return 'logged-out';
      return 'logged-in';
    }

    return 'unknown';
  } finally {
    await page.close().catch(() => undefined);
  }
}

/** Look for a prominent sign-in control, which contradicts an authenticated URL. */
async function hasSignInAffordance(page: Page): Promise<boolean> {
  const candidates = page.getByRole('button', { name: /^(sign in|log in|login)$/i });
  const links = page.getByRole('link', { name: /^(sign in|log in|login)$/i });
  const [buttonVisible, linkVisible] = await Promise.all([
    candidates.first().isVisible({ timeout: 1200 }).catch(() => false),
    links.first().isVisible({ timeout: 1200 }).catch(() => false),
  ]);
  return buttonVisible || linkVisible;
}

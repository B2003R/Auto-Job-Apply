import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { BrowserContext, Locator, Page } from 'playwright';
import { parse as parseYaml } from 'yaml';
import { z } from 'zod';
import { PROJECT_ROOT, loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { pause, scrollThroughPage, sleep, waitForDomToSettle } from '../browser/humanize.ts';
import type { ApplyClickResult, DiscoveredJob, JobSource } from './types.ts';

const boardSelectorsSchema = z.object({
  cardSelectors: z.array(z.string()),
  linkSelectors: z.array(z.string()),
  titleSelectors: z.array(z.string()),
  companySelectors: z.array(z.string()),
  locationSelectors: z.array(z.string()),
  applyNames: z.array(z.string()),
});

const fileSchema = z.record(z.string(), boardSelectorsSchema);

export type BoardSelectors = z.infer<typeof boardSelectorsSchema>;

let cache: z.infer<typeof fileSchema> | null = null;

export function loadBoardSelectors(board: string): BoardSelectors {
  if (!cache) {
    const path = resolve(PROJECT_ROOT, 'config/sources-selectors.yaml');
    const parsed = fileSchema.safeParse(parseYaml(readFileSync(path, 'utf8')));
    if (!parsed.success) {
      throw new Error(`Invalid config/sources-selectors.yaml: ${parsed.error.message}`);
    }
    cache = parsed.data;
  }

  const selectors = cache[board];
  if (!selectors) throw new Error(`No selectors configured for board "${board}"`);
  return selectors;
}

export interface BoardConfig {
  key: string;
  label: string;
  listUrl: string;
  /** Absolute-ise a href found on a card. */
  origin: string;
  /** Extra wait after opening the list, for boards that hydrate slowly. */
  hydrateMs?: number;
}

/**
 * Shared implementation for the job boards.
 *
 * The four boards differ in markup but not in shape: a list of cards, each
 * linking to a posting with an apply control that leads to an ATS. Keeping one
 * adapter means a fix to the apply-and-follow logic benefits every board, and the
 * per-board differences stay in the selector config where they are visible.
 */
export function createBoardSource(board: BoardConfig): JobSource {
  return {
    key: board.key,
    label: board.label,

    async collect(context: BrowserContext, limit: number): Promise<DiscoveredJob[]> {
      const selectors = loadBoardSelectors(board.key);
      const page = await context.newPage();

      try {
        logger.info('Collecting jobs', { source: board.key, listUrl: board.listUrl });
        await page.goto(board.listUrl, { waitUntil: 'domcontentloaded' });
        await waitForDomToSettle(page, 1200, 20_000);
        if (board.hydrateMs) await sleep(board.hydrateMs);

        // Boards lazy-load; scroll enough to reach the requested count.
        for (let round = 0; round < 4; round += 1) {
          const found = await countCards(page, selectors);
          if (found >= limit) break;
          await scrollThroughPage(page, 3);
          await waitForDomToSettle(page, 900, 8000);
        }

        const jobs = await scrapeCards(page, selectors, board, limit);
        logger.info('Collected jobs', { source: board.key, count: jobs.length });
        return jobs;
      } catch (error) {
        logger.warn('Collection failed', {
          source: board.key,
          error: error instanceof Error ? error.message : String(error),
        });
        return [];
      } finally {
        await page.close().catch(() => undefined);
      }
    },

    async clickApply(context: BrowserContext, job: DiscoveredJob): Promise<ApplyClickResult> {
      const selectors = loadBoardSelectors(board.key);
      const config = loadConfig();
      const page = await context.newPage();

      try {
        await page.goto(job.url, { waitUntil: 'domcontentloaded', timeout: config.timeouts.navigation });
        await waitForDomToSettle(page, 1000, 15_000);
        await pause(500, 1400);

        const apply = await findApplyControl(page, selectors);
        if (!apply) {
          return { kind: 'unavailable', note: `No apply control found on the ${board.label} posting` };
        }

        // An apply control usually opens a new tab. Race the popup against
        // same-tab navigation so both shapes are handled without a fixed wait.
        const popupPromise = context.waitForEvent('page', { timeout: 12_000 }).catch(() => null);
        const urlBefore = page.url();

        await apply.click({ timeout: 12_000 }).catch(async () => {
          await apply.click({ force: true, timeout: 8000 });
        });

        const popup = await popupPromise;
        if (popup) {
          await popup.waitForLoadState('domcontentloaded', { timeout: config.timeouts.navigation }).catch(() => undefined);
          await page.close().catch(() => undefined);
          logger.info('Apply opened a new tab', { url: popup.url() });
          return { kind: 'navigated', page: popup, note: `Followed apply to ${popup.url()}` };
        }

        await waitForDomToSettle(page, 1200, 15_000);
        if (page.url() !== urlBefore) {
          return { kind: 'navigated', page, note: `Apply navigated to ${page.url()}` };
        }

        // Neither navigated: the form is probably a modal on the posting itself,
        // which is how LinkedIn Easy Apply and Wellfound behave.
        return { kind: 'same_page', page, note: 'Apply opened an in-page form' };
      } catch (error) {
        await page.close().catch(() => undefined);
        return {
          kind: 'unavailable',
          note: `Failed to open the ${board.label} posting: ${error instanceof Error ? error.message : String(error)}`,
        };
      }
    },
  };
}

async function countCards(page: Page, selectors: BoardSelectors): Promise<number> {
  for (const selector of selectors.cardSelectors) {
    const count = await page.locator(selector).count().catch(() => 0);
    if (count > 0) return count;
  }
  return 0;
}

async function scrapeCards(
  page: Page,
  selectors: BoardSelectors,
  board: BoardConfig,
  limit: number,
): Promise<DiscoveredJob[]> {
  const raw = await page.evaluate(
    ({ cardSelectors, linkSelectors, titleSelectors, companySelectors, locationSelectors, max }) => {
      const text = (el: Element | null): string => (el?.textContent ?? '').replace(/\s+/g, ' ').trim();

      const firstText = (root: Element, candidates: string[]): string => {
        for (const selector of candidates) {
          const found = text(root.querySelector(selector));
          if (found) return found;
        }
        return '';
      };

      let cards: Element[] = [];
      for (const selector of cardSelectors) {
        const found = Array.from(document.querySelectorAll(selector));
        if (found.length > 0) {
          cards = found;
          break;
        }
      }

      const results: Array<{ href: string; title: string; company: string; location: string }> = [];
      const seen = new Set<string>();

      for (const card of cards) {
        if (results.length >= max) break;

        let href = '';
        for (const selector of linkSelectors) {
          const anchor = card.matches(selector)
            ? (card as HTMLAnchorElement)
            : card.querySelector<HTMLAnchorElement>(selector);
          if (anchor?.href) {
            href = anchor.href;
            break;
          }
        }
        if (!href || seen.has(href)) continue;
        seen.add(href);

        results.push({
          href,
          title: firstText(card, titleSelectors),
          company: firstText(card, companySelectors),
          location: firstText(card, locationSelectors),
        });
      }
      return results;
    },
    {
      cardSelectors: selectors.cardSelectors,
      linkSelectors: selectors.linkSelectors,
      titleSelectors: selectors.titleSelectors,
      companySelectors: selectors.companySelectors,
      locationSelectors: selectors.locationSelectors,
      max: limit,
    },
  );

  return raw
    .filter((r) => r.href)
    .map((r) => ({
      source: board.key,
      url: absolute(r.href, board.origin),
      externalId: externalIdFrom(r.href),
      title: r.title || undefined,
      company: r.company || undefined,
      location: r.location || undefined,
    }));
}

/** Find the posting's apply control, preferring an exact accessible-name match. */
async function findApplyControl(page: Page, selectors: BoardSelectors): Promise<Locator | null> {
  for (const name of selectors.applyNames) {
    const pattern = new RegExp(name, 'i');
    for (const role of ['button', 'link'] as const) {
      const locator = page.getByRole(role, { name: pattern }).first();
      if (await locator.isVisible().catch(() => false)) return locator;
    }
  }
  return null;
}

function absolute(href: string, origin: string): string {
  try {
    return new URL(href, origin).toString();
  } catch {
    return href;
  }
}

/** Stable per-board id from the posting URL, used to dedupe re-collections. */
function externalIdFrom(href: string): string | undefined {
  try {
    const path = new URL(href).pathname;
    const digits = path.match(/(\d{5,})/)?.[1];
    if (digits) return digits;
    const last = path.split('/').filter(Boolean).at(-1);
    return last ?? undefined;
  } catch {
    return undefined;
  }
}

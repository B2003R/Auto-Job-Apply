import type { Locator, Page } from 'playwright';

/** Uniform random integer in [min, max]. */
export function randomInt(min: number, max: number): number {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Short pause between discrete UI actions, so bursts do not look scripted. */
export function pause(minMs = 250, maxMs = 900): Promise<void> {
  return sleep(randomInt(minMs, maxMs));
}

/**
 * Type with per-character delay rather than setting value directly. Many ATS
 * forms only run their validation and dependent-field logic on real key events,
 * so this is about correctness as much as it is about looking human.
 */
export async function typeLikeHuman(locator: Locator, text: string): Promise<void> {
  await locator.click();
  await locator.fill('');
  for (const char of text) {
    await locator.press(char === ' ' ? 'Space' : char, { delay: randomInt(18, 75) }).catch(async () => {
      // Fall back to typing for characters Playwright cannot express as a key.
      await locator.type(char, { delay: randomInt(18, 75) });
    });
  }
  await locator.blur().catch(() => undefined);
}

/** Scroll the page in a few steps so lazy-loaded form sections render. */
export async function scrollThroughPage(page: Page, steps = 4): Promise<void> {
  for (let i = 0; i < steps; i += 1) {
    await page.mouse.wheel(0, randomInt(400, 900));
    await sleep(randomInt(180, 480));
  }
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior })).catch(() => undefined);
}

/**
 * Wait until the DOM stops changing, which is a better readiness signal than
 * networkidle on ATS pages that hold long-lived analytics connections open.
 */
export async function waitForDomToSettle(page: Page, quietMs = 700, timeoutMs = 15_000): Promise<void> {
  await page
    .evaluate(
      ([quiet, timeout]) =>
        new Promise<void>((resolve) => {
          let timer: ReturnType<typeof setTimeout>;
          const observer = new MutationObserver(() => {
            clearTimeout(timer);
            timer = setTimeout(finish, quiet);
          });
          const finish = (): void => {
            observer.disconnect();
            resolve();
          };
          observer.observe(document.documentElement, {
            childList: true,
            subtree: true,
            attributes: true,
            characterData: true,
          });
          timer = setTimeout(finish, quiet);
          setTimeout(finish, timeout);
        }),
      [quietMs, timeoutMs] as const,
    )
    .catch(() => undefined);
}

import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { platform } from 'node:os';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { loadConfig } from '../config.ts';
import { logger } from '../logger.ts';

const CHROME_CANDIDATES: Record<string, string[]> = {
  darwin: [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
  ],
  linux: [
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/opt/google/chrome/chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ],
  win32: [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  ],
};

export function resolveChromePath(): string {
  const configured = loadConfig({ requireEnv: false }).env.CHROME_PATH;
  if (configured) {
    if (!existsSync(configured)) {
      throw new Error(`CHROME_PATH points at a missing file: ${configured}`);
    }
    return configured;
  }

  for (const candidate of CHROME_CANDIDATES[platform()] ?? []) {
    if (existsSync(candidate)) return candidate;
  }

  throw new Error(
    `Could not find Google Chrome for platform "${platform()}". Set CHROME_PATH in .env to the Chrome binary.`,
  );
}

export interface BrowserSession {
  browser: Browser;
  context: BrowserContext;
  /** Open a fresh tab, ensuring extension content scripts get a chance to inject. */
  newPage(): Promise<Page>;
  close(): Promise<void>;
}

interface LaunchOptions {
  /** Keep Chrome running after the session closes. Used by the setup command. */
  keepAlive?: boolean;
  /** Land on this URL instead of about:blank when Chrome starts. */
  startUrl?: string;
}

/**
 * Launch Chrome ourselves and attach over CDP.
 *
 * Two constraints force this shape rather than Playwright's launchPersistentContext:
 *
 * 1. The Jobright extension is installed from the Chrome Web Store into a real
 *    profile. Chrome removed the --load-extension side-loading flags, so the only
 *    way to have the extension present is to use a profile that already has it.
 * 2. Chrome 136+ silently ignores --remote-debugging-port when it points at the
 *    *default* profile directory, so the profile must be a dedicated one.
 *
 * Spawning Chrome directly also guarantees Playwright cannot inject its own
 * automation flags that would disable extensions.
 */
export async function launchBrowser(options: LaunchOptions = {}): Promise<BrowserSession> {
  const config = loadConfig({ requireEnv: false });
  const { profileDir } = config.paths;
  const port = config.env.CHROME_DEBUG_PORT;
  const chromePath = resolveChromePath();

  mkdirSync(profileDir, { recursive: true });

  const existing = await probeDebugPort(port);
  let child: ChildProcess | null = null;

  if (existing) {
    logger.info('Attaching to Chrome already listening on the debug port', { port });
  } else {
    child = spawn(
      chromePath,
      [
        `--user-data-dir=${profileDir}`,
        `--remote-debugging-port=${port}`,
        '--no-first-run',
        '--no-default-browser-check',
        // Chrome's own "you are being automated" infobar is absent because we are
        // not using --enable-automation; keep it that way for bot-detection reasons.
        '--disable-features=Translate,MediaRouter',
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        options.startUrl ?? 'about:blank',
      ],
      { detached: false, stdio: 'ignore' },
    );

    child.on('error', (error) => {
      logger.error('Chrome process error', { error: error.message });
    });

    const ready = await waitForDebugPort(port, 30_000);
    if (!ready) {
      child.kill();
      throw new Error(
        `Chrome did not expose the debug port on ${port} within 30s. The usual cause is ` +
          `CHROME_PROFILE_DIR pointing at your default Chrome profile, which Chrome 136+ refuses ` +
          `to debug. Point it at a dedicated directory such as ./.browser-profile.`,
      );
    }
    logger.info('Launched Chrome with dedicated profile', { profileDir, port });
  }

  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
  const context = browser.contexts()[0];
  if (!context) {
    throw new Error('Chrome exposed no browser context over CDP; try closing all Chrome windows and retrying.');
  }

  context.setDefaultTimeout(config.timeouts.elementWait);
  context.setDefaultNavigationTimeout(config.timeouts.navigation);

  return {
    browser,
    context,
    async newPage() {
      return context.newPage();
    },
    async close() {
      // Detach from CDP without killing the browser when asked to keep it alive,
      // so the operator can carry on using the window (setup flow, manual fixes).
      await browser.close().catch(() => undefined);
      if (!options.keepAlive && child) {
        await terminate(child);
      }
    },
  };
}

/** List the extension IDs Chrome has loaded, read from their service workers. */
export async function listExtensionIds(context: BrowserContext): Promise<string[]> {
  const ids = new Set<string>();
  for (const worker of context.serviceWorkers()) {
    const match = /^chrome-extension:\/\/([a-p]{32})\//.exec(worker.url());
    if (match?.[1]) ids.add(match[1]);
  }
  return [...ids];
}

/**
 * Stop Chrome and wait for the process to actually exit.
 *
 * Returning before exit lets the next run collide with a shutting-down Chrome
 * that still holds the profile lock and the debug port, which surfaces as a
 * confusing "could not expose debug port" failure.
 */
async function terminate(child: ChildProcess, graceMs = 5000): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;

  const exited = new Promise<void>((resolve) => child.once('exit', () => resolve()));
  child.kill('SIGTERM');

  const timedOut = await Promise.race([
    exited.then(() => false),
    new Promise<boolean>((resolve) => setTimeout(() => resolve(true), graceMs)),
  ]);

  if (timedOut) {
    logger.warn('Chrome did not exit on SIGTERM; sending SIGKILL');
    child.kill('SIGKILL');
    await exited;
  }
}

async function probeDebugPort(port: number): Promise<boolean> {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/json/version`, {
      signal: AbortSignal.timeout(1500),
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function waitForDebugPort(port: number, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await probeDebugPort(port)) return true;
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

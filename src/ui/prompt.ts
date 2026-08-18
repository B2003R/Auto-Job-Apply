import { createInterface } from 'node:readline/promises';
import { stdin, stdout } from 'node:process';

/** Ask a free-text question. Returns the trimmed answer. */
export async function ask(question: string): Promise<string> {
  const rl = createInterface({ input: stdin, output: stdout });
  try {
    return (await rl.question(question)).trim();
  } finally {
    rl.close();
  }
}

export async function waitForEnter(message = 'Press Enter to continue... '): Promise<void> {
  await ask(message);
}

/** Yes/no prompt. Anything other than an explicit yes is treated as no. */
export async function confirm(question: string, defaultYes = false): Promise<boolean> {
  const suffix = defaultYes ? '[Y/n] ' : '[y/N] ';
  const answer = (await ask(`${question} ${suffix}`)).toLowerCase();
  if (answer === '') return defaultYes;
  return answer === 'y' || answer === 'yes';
}

/**
 * Wait for the operator to resolve something in the browser, with a deadline so
 * an unattended run cannot stall forever. Resolves false on timeout.
 */
export async function waitForEnterWithTimeout(message: string, timeoutMs: number): Promise<boolean> {
  const rl = createInterface({ input: stdin, output: stdout });
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      await rl.question(message, { signal: controller.signal });
      return true;
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  } finally {
    rl.close();
  }
}

export const heading = (text: string): string => `\n${text}\n${'─'.repeat(Math.max(text.length, 40))}`;

import { loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { launchBrowser } from '../browser/chrome.ts';
import { randomInt, sleep } from '../browser/humanize.ts';
import { detectJobrightExtension } from '../jobright/extension.ts';
import { applyToJob, type ApplicationResult } from '../apply/orchestrator.ts';
import { buildSources } from '../sources/index.ts';
import {
  claimQueuedJobs,
  recordEvent,
  submittedToday,
  submittedTodayBySource,
  type JobRow,
} from '../store/db.ts';
import { writeDailyExport } from '../export/daily.ts';
import type { CliArgs } from '../index.ts';

/**
 * Work the queue for the day.
 *
 * The loop is written to fail safe: a per-source budget derived from what has
 * already been submitted today, a circuit breaker on consecutive failures, and an
 * export written even when the run ends badly, so a crashed run still leaves the
 * operator the URLs it did not finish.
 */
export async function runApplications(args: CliArgs): Promise<void> {
  const config = loadConfig();
  const sources = buildSources().filter((s) => !args.source || s.key === args.source);
  const sourcesByKey = new Map(sources.map((s) => [s.key, s]));

  if (sources.length === 0) {
    process.stdout.write('No sources are enabled in config/config.yaml.\n');
    return;
  }

  const alreadyDone = submittedToday();
  const remaining = Math.max(0, config.daily.target - alreadyDone);
  const budget = Math.min(args.limit ?? remaining, remaining);

  if (budget === 0) {
    process.stdout.write(
      `Daily target already met: ${alreadyDone} of ${config.daily.target} submitted today.\n`,
    );
    return;
  }

  const perSourceBudget = computeSourceBudgets(sourcesByKey.keys(), config, submittedTodayBySource());
  const jobs = claimQueuedJobs(budget * 2, perSourceBudget);

  if (jobs.length === 0) {
    process.stdout.write('Queue is empty. Run `npm run collect` first.\n');
    return;
  }

  process.stdout.write(
    [
      '',
      `Mode              ${args.mode}${args.mode === 'dry-run' ? '  (nothing will be submitted)' : ''}`,
      `Submitted today   ${alreadyDone} / ${config.daily.target}`,
      `Budget this run   ${budget}`,
      `Queued candidates ${jobs.length}`,
      '',
    ].join('\n'),
  );

  const session = await launchBrowser({ keepAlive: true });
  const results: ApplicationResult[] = [];
  let consecutiveFailures = 0;
  let submitted = 0;

  try {
    const extension = await detectJobrightExtension(session.context);
    if (!extension.installed) {
      logger.warn(
        'Jobright extension not detected in the agent profile. Forms will be filled from the answer bank only, which is slower and more likely to need a human.',
      );
    }

    for (const job of jobs) {
      if (submitted >= budget) break;

      if (consecutiveFailures >= config.safety.circuitBreakerFailures) {
        // Repeated failures usually mean something systemic (a stale selector, a
        // signed-out session). Continuing would burn the day's quota learning
        // nothing new.
        logger.error('Circuit breaker tripped; stopping the run', { consecutiveFailures });
        recordEvent('CIRCUIT_BREAKER', { message: `Stopped after ${consecutiveFailures} consecutive failures` });
        process.stdout.write(
          `\nStopped after ${consecutiveFailures} consecutive failures. Check data/logs and re-run once fixed.\n`,
        );
        break;
      }

      const source = sourcesByKey.get(job.source);
      if (!source) continue;

      process.stdout.write(`\n→ ${describe(job)}\n`);

      const result = await applyToJob(session.context, source, job, {
        mode: args.mode,
        interactive: args.mode !== 'auto' || process.stdin.isTTY === true,
      });
      results.push(result);

      const succeeded = result.finalState === 'submitted' || result.finalState === 'dry_run_complete';
      if (result.finalState === 'submitted') submitted += 1;

      // Only hard failures trip the breaker. A closed posting or a CAPTCHA is a
      // normal outcome, not a sign the agent is broken.
      if (result.finalState === 'failed') consecutiveFailures += 1;
      else consecutiveFailures = 0;

      process.stdout.write(`  ${succeeded ? 'ok' : result.finalState}  ${result.detail}\n`);

      if (submitted < budget) {
        const gap = randomInt(config.daily.gapSeconds.min, config.daily.gapSeconds.max);
        logger.debug('Pacing before the next application', { seconds: gap });
        await sleep(gap * 1000);
      }
    }
  } finally {
    await session.close();

    // Always export: a run that ends badly is exactly when the operator most
    // needs the list of URLs it did not finish.
    const exported = writeDailyExport();
    printSummary(results, exported.dir);
  }
}

/** Remaining allowance per source, so one board cannot consume the whole day. */
export function computeSourceBudgets(
  keys: Iterable<string>,
  config: ReturnType<typeof loadConfig>,
  submittedBySource: Record<string, number>,
): Record<string, number> {
  const budgets: Record<string, number> = {};
  for (const key of keys) {
    const entry = (config.sources as Record<string, { cap: number } | undefined>)[key];
    const cap = entry?.cap ?? 0;
    budgets[key] = Math.max(0, cap - (submittedBySource[key] ?? 0));
  }
  return budgets;
}

function describe(job: JobRow): string {
  return [job.title, job.company].filter(Boolean).join(' — ') || job.url;
}

function printSummary(results: ApplicationResult[], exportDir: string): void {
  const counts = new Map<string, number>();
  for (const r of results) counts.set(r.finalState, (counts.get(r.finalState) ?? 0) + 1);

  process.stdout.write('\nRun summary\n');
  if (results.length === 0) {
    process.stdout.write('  nothing processed\n');
  }
  for (const [state, count] of [...counts.entries()].sort()) {
    process.stdout.write(`  ${state.padEnd(18)} ${count}\n`);
  }
  process.stdout.write(`\nExport written to ${exportDir}\n\n`);
}

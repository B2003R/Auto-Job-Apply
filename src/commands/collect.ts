import { loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { launchBrowser } from '../browser/chrome.ts';
import { buildSources } from '../sources/index.ts';
import { recordEvent, upsertJob } from '../store/db.ts';
import type { CliArgs } from '../index.ts';

/**
 * Fill the queue from the enabled boards.
 *
 * Collection is separated from applying so the queue can be reviewed before
 * anything is submitted, and so a board being temporarily broken does not stall
 * an application run that already has work to do.
 */
export async function collectJobs(args: CliArgs): Promise<void> {
  const config = loadConfig({ requireEnv: false });
  const sources = buildSources().filter((s) => !args.source || s.key === args.source);

  if (sources.length === 0) {
    process.stdout.write(
      args.source
        ? `Source "${args.source}" is not enabled in config/config.yaml.\n`
        : 'No sources are enabled in config/config.yaml.\n',
    );
    return;
  }

  const session = await launchBrowser({ keepAlive: true });
  const summary: Array<{ source: string; found: number; added: number }> = [];

  try {
    for (const source of sources) {
      // Collect more than the cap so the queue survives postings that turn out to
      // be closed or unapplyable.
      const target = Math.max(args.limit ?? 0, sourceCapFor(source.key, config) * 2, 10);
      const jobs = await source.collect(session.context, target);

      let added = 0;
      for (const job of jobs) {
        const { inserted } = upsertJob(job);
        if (inserted) added += 1;
      }

      summary.push({ source: source.key, found: jobs.length, added });
      recordEvent('COLLECTED', { message: source.key, data: { found: jobs.length, added } });
    }
  } finally {
    await session.close();
  }

  process.stdout.write('\nCollection summary\n');
  for (const row of summary) {
    process.stdout.write(`  ${row.source.padEnd(12)} found ${String(row.found).padStart(3)}  new ${String(row.added).padStart(3)}\n`);
  }
  const totalAdded = summary.reduce((sum, r) => sum + r.added, 0);
  process.stdout.write(`\n${totalAdded} new job(s) queued. Next: npm run run:dry\n\n`);
  logger.info('Collection finished', { totalAdded });
}

function sourceCapFor(key: string, config: ReturnType<typeof loadConfig>): number {
  const entry = (config.sources as Record<string, { cap: number } | undefined>)[key];
  return entry?.cap ?? 0;
}

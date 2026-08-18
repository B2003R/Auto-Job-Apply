import { parseArgs } from 'node:util';
import { runModes, type RunMode } from './config.ts';
import { logger } from './logger.ts';
import { closeDb } from './store/db.ts';
import { printStatus } from './commands/status.ts';
import { setupBrowser } from './commands/setupBrowser.ts';
import { collectJobs } from './commands/collect.ts';
import { runApplications } from './commands/run.ts';
import { exportDay } from './commands/exportDay.ts';
import { vaultExport } from './commands/vaultExport.ts';

interface Command {
  describe: string;
  run(args: CliArgs): Promise<void>;
}

export interface CliArgs {
  mode: RunMode;
  limit?: number;
  source?: string;
  date?: string;
}

const commands: Record<string, Command> = {
  'setup:browser': {
    describe: 'One-time: provision the dedicated Chrome profile and sign in',
    run: async () => setupBrowser(),
  },
  collect: {
    describe: 'Fill the queue from the enabled job boards',
    run: async (args) => collectJobs(args),
  },
  run: {
    describe: 'Work the queue, applying up to the daily target',
    run: async (args) => runApplications(args),
  },
  export: {
    describe: 'Write the day\'s CSV report and failed-URL list',
    run: async (args) => exportDay(args),
  },
  status: {
    describe: 'Show queue depth, today\'s counters, and recent failures',
    run: async () => printStatus(),
  },
  'vault:export': {
    describe: 'Print ATS accounts the agent created, with passwords',
    run: async () => vaultExport(),
  },
};

function usage(): string {
  const lines = Object.entries(commands).map(([name, c]) => `  ${name.padEnd(16)} ${c.describe}`);
  return [
    'Usage: npm run agent -- <command> [options]',
    '',
    'Commands:',
    ...lines,
    '',
    'Options:',
    '  --mode <dry-run|review|auto>  Submission behaviour (default: dry-run)',
    '  --limit <n>                   Cap jobs processed this invocation',
    '  --source <name>               Restrict to one source',
    '  --date <YYYY-MM-DD>           Target date for export',
    '',
  ].join('\n');
}

async function main(): Promise<void> {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      mode: { type: 'string', default: 'dry-run' },
      limit: { type: 'string' },
      source: { type: 'string' },
      date: { type: 'string' },
      help: { type: 'boolean', short: 'h', default: false },
    },
  });

  const commandName = positionals[0];
  if (values.help || !commandName) {
    process.stdout.write(usage());
    return;
  }

  const command = commands[commandName];
  if (!command) {
    process.stderr.write(`Unknown command: ${commandName}\n\n${usage()}`);
    process.exitCode = 1;
    return;
  }

  if (!runModes.includes(values.mode as RunMode)) {
    process.stderr.write(`Invalid --mode "${values.mode}". Expected one of: ${runModes.join(', ')}\n`);
    process.exitCode = 1;
    return;
  }

  const args: CliArgs = {
    mode: values.mode as RunMode,
    limit: values.limit ? Number(values.limit) : undefined,
    source: values.source,
    date: values.date,
  };

  if (args.limit !== undefined && (!Number.isInteger(args.limit) || args.limit <= 0)) {
    process.stderr.write('--limit must be a positive integer\n');
    process.exitCode = 1;
    return;
  }

  await command.run(args);
}

main()
  .catch((error: unknown) => {
    logger.error('Command failed', { error: error instanceof Error ? error.message : String(error) });
    if (error instanceof Error && error.stack && process.env.LOG_LEVEL === 'debug') {
      process.stderr.write(`${error.stack}\n`);
    }
    process.exitCode = 1;
  })
  .finally(() => {
    closeDb();
  });

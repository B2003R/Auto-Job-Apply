import { today } from '../store/db.ts';
import { writeDailyExport } from '../export/daily.ts';
import type { CliArgs } from '../index.ts';

export function exportDay(args: CliArgs): void {
  const date = args.date ?? today();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new Error(`--date must be YYYY-MM-DD, got "${date}"`);
  }

  const result = writeDailyExport(date);
  process.stdout.write(
    [
      '',
      `Export for ${date}`,
      `  submitted         ${result.submitted}`,
      `  needing attention ${result.unfinished}`,
      '',
      ...result.files.map((f) => `  ${f}`),
      '',
    ].join('\n'),
  );
}

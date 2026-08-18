import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { getDb, today } from '../store/db.ts';

interface AttemptExportRow {
  started_at: string;
  finished_at: string | null;
  source: string;
  company: string | null;
  title: string | null;
  location: string | null;
  job_url: string;
  final_url: string | null;
  ats_vendor: string | null;
  mode: string;
  final_state: string | null;
  failure_class: string | null;
  failure_message: string | null;
  steps_completed: number;
  submitted: number;
  screenshot: string | null;
}

export interface ExportResult {
  dir: string;
  files: string[];
  submitted: number;
  unfinished: number;
}

/**
 * Write the day's record to disk.
 *
 * Two files matter operationally: applications.csv is the full history, and
 * failures.csv is the actionable one, carrying every URL that did not complete
 * along with the stage it stopped at and a screenshot to look at. Anything needing
 * a human is included there, not just hard errors, since from the operator's
 * point of view both are "applications I still have to deal with".
 */
export function writeDailyExport(date = today()): ExportResult {
  const { paths } = loadConfig({ requireEnv: false });
  const dir = join(paths.exportsDir, date);
  mkdirSync(dir, { recursive: true });

  const rows = getDb()
    .prepare(
      `SELECT a.started_at, a.finished_at, j.source, j.company, j.title, j.location,
              j.url AS job_url, a.final_url, a.ats_vendor, a.mode, a.final_state,
              a.failure_class, a.failure_message, a.steps_completed, a.submitted,
              (SELECT path FROM artifacts ar WHERE ar.attempt_id = a.id ORDER BY ar.id DESC LIMIT 1) AS screenshot
         FROM attempts a
         JOIN jobs j ON j.id = a.job_id
        WHERE date(a.started_at) = ?
        ORDER BY a.id`,
    )
    .all(date) as AttemptExportRow[];

  const header = [
    'started_at',
    'finished_at',
    'source',
    'company',
    'title',
    'location',
    'job_url',
    'final_url',
    'ats_vendor',
    'mode',
    'outcome',
    'failure_class',
    'failure_message',
    'steps_completed',
    'screenshot',
  ];

  const toRow = (r: AttemptExportRow): string[] => [
    r.started_at,
    r.finished_at ?? '',
    r.source,
    r.company ?? '',
    r.title ?? '',
    r.location ?? '',
    r.job_url,
    r.final_url ?? '',
    r.ats_vendor ?? '',
    r.mode,
    r.final_state ?? 'incomplete',
    r.failure_class ?? '',
    r.failure_message ?? '',
    String(r.steps_completed),
    r.screenshot ?? '',
  ];

  const files: string[] = [];

  const applicationsPath = join(dir, 'applications.csv');
  writeFileSync(applicationsPath, toCsv([header, ...rows.map(toRow)]));
  files.push(applicationsPath);

  const unfinished = rows.filter((r) => r.submitted === 0 && r.final_state !== 'dry_run_complete');
  const failuresPath = join(dir, 'failures.csv');
  writeFileSync(failuresPath, toCsv([header, ...unfinished.map(toRow)]));
  files.push(failuresPath);

  // A bare URL list, because pasting these into a browser is the most common
  // thing an operator wants to do with them.
  const urlsPath = join(dir, 'failed-urls.txt');
  writeFileSync(
    urlsPath,
    unfinished.map((r) => r.final_url || r.job_url).join('\n') + (unfinished.length > 0 ? '\n' : ''),
  );
  files.push(urlsPath);

  const summaryPath = join(dir, 'summary.md');
  writeFileSync(summaryPath, buildSummary(date, rows, unfinished));
  files.push(summaryPath);

  const submitted = rows.filter((r) => r.submitted === 1).length;
  logger.info('Daily export written', { dir, attempts: rows.length, submitted, unfinished: unfinished.length });

  return { dir, files, submitted, unfinished: unfinished.length };
}

function buildSummary(date: string, rows: AttemptExportRow[], unfinished: AttemptExportRow[]): string {
  const submitted = rows.filter((r) => r.submitted === 1);
  const byState = tally(rows.map((r) => r.final_state ?? 'incomplete'));
  const byFailure = tally(unfinished.map((r) => r.failure_class ?? 'unknown'));
  const bySource = tally(rows.map((r) => r.source));

  const lines: string[] = [
    `# Application report — ${date}`,
    '',
    `Attempts: ${rows.length}`,
    `Submitted: ${submitted.length}`,
    `Needing attention: ${unfinished.length}`,
    '',
    '## Outcomes',
    '',
    ...Object.entries(byState).map(([k, v]) => `- ${k}: ${v}`),
    '',
    '## By source',
    '',
    ...Object.entries(bySource).map(([k, v]) => `- ${k}: ${v}`),
  ];

  if (Object.keys(byFailure).length > 0) {
    lines.push('', '## Why applications did not complete', '', ...Object.entries(byFailure).map(([k, v]) => `- ${k}: ${v}`));
  }

  if (submitted.length > 0) {
    lines.push('', '## Submitted', '');
    for (const r of submitted) {
      lines.push(`- ${[r.company, r.title].filter(Boolean).join(' — ') || r.job_url} (${r.source})`);
    }
  }

  if (unfinished.length > 0) {
    lines.push('', '## Needs attention', '');
    for (const r of unfinished) {
      const label = [r.company, r.title].filter(Boolean).join(' — ') || r.job_url;
      lines.push(`- ${label}`);
      lines.push(`  - ${r.final_url || r.job_url}`);
      lines.push(`  - ${r.failure_class ?? 'unknown'}: ${r.failure_message ?? 'no detail recorded'}`);
      if (r.screenshot) lines.push(`  - screenshot: ${r.screenshot}`);
    }
  }

  const unanswered = getDb()
    .prepare(`SELECT question, seen_count FROM unanswered_questions ORDER BY seen_count DESC LIMIT 20`)
    .all() as Array<{ question: string; seen_count: number }>;

  if (unanswered.length > 0) {
    lines.push(
      '',
      '## Questions to add to config/answers.yaml',
      '',
      'Each of these blocked at least one application. Adding an answer stops it recurring.',
      '',
      ...unanswered.map((q) => `- (${q.seen_count}x) ${q.question}`),
    );
  }

  return `${lines.join('\n')}\n`;
}

function tally(values: string[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const value of values) counts[value] = (counts[value] ?? 0) + 1;
  return Object.fromEntries(Object.entries(counts).sort((a, b) => b[1] - a[1]));
}

/** RFC 4180 quoting: fields are wrapped and inner quotes doubled. */
export function toCsv(rows: string[][]): string {
  return `${rows.map((row) => row.map(escapeCsv).join(',')).join('\n')}\n`;
}

function escapeCsv(value: string): string {
  const normalised = value.replace(/\r?\n/g, ' ').trim();
  if (/["',]/.test(normalised)) return `"${normalised.replace(/"/g, '""')}"`;
  return normalised;
}

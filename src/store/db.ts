import Database from 'better-sqlite3';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { loadConfig } from '../config.ts';

export type JobStatus = 'queued' | 'in_progress' | 'submitted' | 'failed' | 'blocked_human' | 'skipped';

export interface JobRow {
  id: string;
  source: string;
  external_id: string | null;
  url: string;
  apply_url: string | null;
  company: string | null;
  title: string | null;
  location: string | null;
  dedupe_key: string;
  status: JobStatus;
  attempt_count: number;
  discovered_at: string;
  updated_at: string;
}

export interface NewJob {
  source: string;
  externalId?: string;
  url: string;
  company?: string;
  title?: string;
  location?: string;
}

export interface AttemptRow {
  id: number;
  job_id: string;
  mode: string;
  started_at: string;
  finished_at: string | null;
  final_state: string | null;
  ats_vendor: string | null;
  final_url: string | null;
  failure_class: string | null;
  failure_message: string | null;
  steps_completed: number;
  submitted: number;
}

let db: Database.Database | null = null;

export function getDb(): Database.Database {
  if (db) return db;
  const { paths } = loadConfig({ requireEnv: false });
  mkdirSync(dirname(paths.dbFile), { recursive: true });
  db = new Database(paths.dbFile);
  db.pragma('busy_timeout = 5000');
  db.exec(readFileSync(resolve(import.meta.dirname, 'schema.sql'), 'utf8'));
  return db;
}

export function closeDb(): void {
  db?.close();
  db = null;
}

const nowIso = (): string => new Date().toISOString();

export const today = (): string => {
  // Local date, not UTC: "50 per day" means the operator's day.
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

/**
 * Fingerprint used to suppress duplicates. Deliberately excludes the source so
 * the same role found on both LinkedIn and Jobright collapses to one job, and
 * excludes the URL because boards decorate links with tracking parameters.
 */
export function dedupeKey(company: string | undefined, title: string | undefined, url: string): string {
  const norm = (s: string): string => s.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const basis = company && title ? `${norm(company)}|${norm(title)}` : `url:${stripTracking(url)}`;
  return createHash('sha1').update(basis).digest('hex');
}

function stripTracking(url: string): string {
  try {
    const u = new URL(url);
    for (const key of [...u.searchParams.keys()]) {
      if (/^(utm_|ref|refId|trk|trackingId|gh_src|source)/i.test(key)) u.searchParams.delete(key);
    }
    return `${u.origin}${u.pathname}${u.search}`.toLowerCase();
  } catch {
    return url.toLowerCase();
  }
}

/** Insert a job unless an equivalent one already exists. Returns true if new. */
export function upsertJob(job: NewJob): { id: string; inserted: boolean } {
  const key = dedupeKey(job.company, job.title, job.url);
  const id = createHash('sha1').update(`${job.source}:${job.externalId ?? job.url}`).digest('hex').slice(0, 16);
  const ts = nowIso();

  const result = getDb()
    .prepare(
      `INSERT INTO jobs (id, source, external_id, url, company, title, location, dedupe_key, discovered_at, updated_at)
       VALUES (@id, @source, @externalId, @url, @company, @title, @location, @key, @ts, @ts)
       ON CONFLICT (dedupe_key) DO NOTHING`,
    )
    .run({
      id,
      source: job.source,
      externalId: job.externalId ?? null,
      url: job.url,
      company: job.company ?? null,
      title: job.title ?? null,
      location: job.location ?? null,
      key,
      ts,
    });

  if (result.changes > 0) return { id, inserted: true };

  const existing = getDb().prepare('SELECT id FROM jobs WHERE dedupe_key = ?').get(key) as { id: string } | undefined;
  return { id: existing?.id ?? id, inserted: false };
}

/** Claim the next queued jobs, honouring per-source daily caps. */
export function claimQueuedJobs(limit: number, sourceBudget: Record<string, number>): JobRow[] {
  const rows = getDb()
    .prepare(`SELECT * FROM jobs WHERE status = 'queued' ORDER BY discovered_at ASC`)
    .all() as JobRow[];

  const picked: JobRow[] = [];
  const used: Record<string, number> = {};
  for (const row of rows) {
    if (picked.length >= limit) break;
    const budget = sourceBudget[row.source] ?? 0;
    const spent = used[row.source] ?? 0;
    if (spent >= budget) continue;
    used[row.source] = spent + 1;
    picked.push(row);
  }
  return picked;
}

export function setJobStatus(jobId: string, status: JobStatus, applyUrl?: string): void {
  getDb()
    .prepare(
      `UPDATE jobs SET status = ?, updated_at = ?, apply_url = COALESCE(?, apply_url) WHERE id = ?`,
    )
    .run(status, nowIso(), applyUrl ?? null, jobId);
}

export function startAttempt(jobId: string, mode: string): number {
  const db = getDb();
  const info = db
    .prepare(`INSERT INTO attempts (job_id, mode, started_at) VALUES (?, ?, ?)`)
    .run(jobId, mode, nowIso());
  db.prepare(`UPDATE jobs SET attempt_count = attempt_count + 1, status = 'in_progress', updated_at = ? WHERE id = ?`)
    .run(nowIso(), jobId);
  return Number(info.lastInsertRowid);
}

export function finishAttempt(
  attemptId: number,
  fields: {
    finalState: string;
    submitted: boolean;
    atsVendor?: string | null;
    finalUrl?: string | null;
    failureClass?: string | null;
    failureMessage?: string | null;
    stepsCompleted?: number;
  },
): void {
  getDb()
    .prepare(
      `UPDATE attempts
          SET finished_at = @ts, final_state = @finalState, submitted = @submitted,
              ats_vendor = @atsVendor, final_url = @finalUrl,
              failure_class = @failureClass, failure_message = @failureMessage,
              steps_completed = @stepsCompleted
        WHERE id = @id`,
    )
    .run({
      id: attemptId,
      ts: nowIso(),
      finalState: fields.finalState,
      submitted: fields.submitted ? 1 : 0,
      atsVendor: fields.atsVendor ?? null,
      finalUrl: fields.finalUrl ?? null,
      failureClass: fields.failureClass ?? null,
      failureMessage: fields.failureMessage ?? null,
      stepsCompleted: fields.stepsCompleted ?? 0,
    });
}

export function recordEvent(
  state: string,
  opts: { jobId?: string; attemptId?: number; message?: string; data?: unknown } = {},
): void {
  getDb()
    .prepare(`INSERT INTO events (job_id, attempt_id, ts, state, message, data) VALUES (?, ?, ?, ?, ?, ?)`)
    .run(
      opts.jobId ?? null,
      opts.attemptId ?? null,
      nowIso(),
      state,
      opts.message ?? null,
      opts.data === undefined ? null : JSON.stringify(opts.data),
    );
}

export function recordArtifact(kind: string, path: string, opts: { jobId?: string; attemptId?: number } = {}): void {
  getDb()
    .prepare(`INSERT INTO artifacts (job_id, attempt_id, kind, path, ts) VALUES (?, ?, ?, ?, ?)`)
    .run(opts.jobId ?? null, opts.attemptId ?? null, kind, path, nowIso());
}

export function recordUnansweredQuestion(q: {
  jobId?: string;
  domain?: string;
  question: string;
  fieldType?: string;
  options?: string[];
}): void {
  getDb()
    .prepare(
      `INSERT INTO unanswered_questions (job_id, domain, question, field_type, options, first_seen, last_seen)
       VALUES (@jobId, @domain, @question, @fieldType, @options, @ts, @ts)
       ON CONFLICT (question) DO UPDATE SET seen_count = seen_count + 1, last_seen = @ts`,
    )
    .run({
      jobId: q.jobId ?? null,
      domain: q.domain ?? null,
      question: q.question.slice(0, 500),
      fieldType: q.fieldType ?? null,
      options: q.options ? JSON.stringify(q.options) : null,
      ts: nowIso(),
    });
}

export function bumpCounter(source: string, field: 'submitted' | 'attempted'): void {
  getDb()
    .prepare(
      `INSERT INTO daily_counters (day, source, ${field}) VALUES (?, ?, 1)
       ON CONFLICT (day, source) DO UPDATE SET ${field} = ${field} + 1`,
    )
    .run(today(), source);
}

export function submittedToday(): number {
  const row = getDb()
    .prepare(`SELECT COALESCE(SUM(submitted), 0) AS n FROM daily_counters WHERE day = ?`)
    .get(today()) as { n: number };
  return row.n;
}

export function submittedTodayBySource(): Record<string, number> {
  const rows = getDb()
    .prepare(`SELECT source, submitted FROM daily_counters WHERE day = ?`)
    .all(today()) as Array<{ source: string; submitted: number }>;
  return Object.fromEntries(rows.map((r) => [r.source, r.submitted]));
}

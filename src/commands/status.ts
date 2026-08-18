import { loadConfig } from '../config.ts';
import { getDb, submittedToday, submittedTodayBySource, today } from '../store/db.ts';

interface CountRow {
  status: string;
  n: number;
}

interface FailureRow {
  company: string | null;
  title: string | null;
  url: string;
  final_state: string | null;
  failure_class: string | null;
  failure_message: string | null;
}

/** Operator dashboard: what is queued, what shipped today, what is stuck. */
export function printStatus(): void {
  const config = loadConfig({ requireEnv: false });
  const db = getDb();

  const counts = db.prepare(`SELECT status, COUNT(*) AS n FROM jobs GROUP BY status`).all() as CountRow[];
  const byStatus = Object.fromEntries(counts.map((c) => [c.status, c.n]));
  const done = submittedToday();
  const bySource = submittedTodayBySource();

  const out: string[] = [];
  out.push('');
  out.push(`Auto-apply status  ${today()}`);
  out.push('─'.repeat(52));
  out.push(`Submitted today     ${done} / ${config.daily.target}`);

  for (const [name, source] of Object.entries(config.sources)) {
    if (!source.enabled) continue;
    out.push(`  ${name.padEnd(16)} ${bySource[name] ?? 0} / ${source.cap}`);
  }

  out.push('');
  out.push('Queue');
  for (const status of ['queued', 'in_progress', 'submitted', 'failed', 'blocked_human', 'skipped']) {
    out.push(`  ${status.padEnd(16)} ${byStatus[status] ?? 0}`);
  }

  const failures = db
    .prepare(
      `SELECT j.company, j.title, j.url, a.final_state, a.failure_class, a.failure_message
         FROM attempts a JOIN jobs j ON j.id = a.job_id
        WHERE a.submitted = 0 AND a.finished_at IS NOT NULL
        ORDER BY a.id DESC LIMIT 10`,
    )
    .all() as FailureRow[];

  if (failures.length > 0) {
    out.push('');
    out.push('Recent unfinished attempts');
    for (const f of failures) {
      const label = [f.company, f.title].filter(Boolean).join(' — ') || f.url;
      out.push(`  ${(f.failure_class ?? f.final_state ?? 'unknown').padEnd(22)} ${label}`);
      if (f.failure_message) out.push(`  ${' '.repeat(22)} ${f.failure_message.slice(0, 96)}`);
    }
  }

  const unanswered = db
    .prepare(`SELECT question, seen_count FROM unanswered_questions ORDER BY seen_count DESC LIMIT 8`)
    .all() as Array<{ question: string; seen_count: number }>;

  if (unanswered.length > 0) {
    out.push('');
    out.push('Questions with no answer-bank entry (add these to config/answers.yaml)');
    for (const q of unanswered) {
      out.push(`  ${String(q.seen_count).padStart(3)}x  ${q.question.slice(0, 88)}`);
    }
  }

  out.push('');
  process.stdout.write(`${out.join('\n')}\n`);
}

import { appendFileSync, mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { PROJECT_ROOT } from './config.ts';

export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

const levelRank: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };
const minLevel: LogLevel = (process.env.LOG_LEVEL as LogLevel) ?? 'info';

const colors: Record<LogLevel, string> = {
  debug: '\x1b[90m',
  info: '\x1b[36m',
  warn: '\x1b[33m',
  error: '\x1b[31m',
};
const RESET = '\x1b[0m';

const logDir = resolve(PROJECT_ROOT, 'data/logs');
let logFile: string | null = null;

function ensureLogFile(): string {
  if (!logFile) {
    mkdirSync(logDir, { recursive: true });
    logFile = resolve(logDir, `${new Date().toISOString().slice(0, 10)}.jsonl`);
  }
  return logFile;
}

export interface Logger {
  debug(message: string, fields?: Record<string, unknown>): void;
  info(message: string, fields?: Record<string, unknown>): void;
  warn(message: string, fields?: Record<string, unknown>): void;
  error(message: string, fields?: Record<string, unknown>): void;
  child(bindings: Record<string, unknown>): Logger;
}

function write(level: LogLevel, bindings: Record<string, unknown>, message: string, fields?: Record<string, unknown>): void {
  if (levelRank[level] < levelRank[minLevel]) return;

  const entry = { ts: new Date().toISOString(), level, message, ...bindings, ...fields };

  const context = Object.entries({ ...bindings, ...fields })
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(' ');
  const line = `${colors[level]}${level.toUpperCase().padEnd(5)}${RESET} ${message}${context ? `  ${'\x1b[90m'}${context}${RESET}` : ''}`;
  (level === 'error' || level === 'warn' ? process.stderr : process.stdout).write(`${line}\n`);

  // Structured sink is best-effort: a failure to journal must never take down a run.
  try {
    appendFileSync(ensureLogFile(), `${JSON.stringify(entry)}\n`);
  } catch {
    /* ignore */
  }
}

function build(bindings: Record<string, unknown>): Logger {
  return {
    debug: (m, f) => write('debug', bindings, m, f),
    info: (m, f) => write('info', bindings, m, f),
    warn: (m, f) => write('warn', bindings, m, f),
    error: (m, f) => write('error', bindings, m, f),
    child: (extra) => build({ ...bindings, ...extra }),
  };
}

export const logger: Logger = build({});

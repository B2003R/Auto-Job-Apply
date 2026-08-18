import { loadConfig } from '../config.ts';
import { createBoardSource } from './boardAdapter.ts';
import type { JobSource } from './types.ts';

/**
 * The four boards, in the order they are worth trusting.
 *
 * Jobright first: it supplies the recommendations and its own extension handles
 * the destination ATS, so it has the highest completion rate. Handshake is
 * disabled by default because it is university-SSO gated and Jobright autofill
 * is unlikely to support it, so it escalates to a human more often than it
 * finishes.
 */
export function buildSources(): JobSource[] {
  const config = loadConfig({ requireEnv: false });
  const sources: JobSource[] = [];

  if (config.sources.jobright.enabled) {
    sources.push(
      createBoardSource({
        key: 'jobright',
        label: 'Jobright',
        listUrl: config.sources.jobright.listUrl,
        origin: 'https://jobright.ai',
        hydrateMs: 2500,
      }),
    );
  }

  if (config.sources.linkedin.enabled) {
    const filter = config.sources.linkedin.onlyEasyApply ? '&f_AL=true' : '';
    sources.push(
      createBoardSource({
        key: 'linkedin',
        label: 'LinkedIn',
        listUrl: `https://www.linkedin.com/jobs/collections/recommended/?discover=recommended${filter}`,
        origin: 'https://www.linkedin.com',
        hydrateMs: 3000,
      }),
    );
  }

  if (config.sources.wellfound.enabled) {
    sources.push(
      createBoardSource({
        key: 'wellfound',
        label: 'Wellfound',
        listUrl: 'https://wellfound.com/jobs',
        origin: 'https://wellfound.com',
        hydrateMs: 2500,
      }),
    );
  }

  if (config.sources.handshake.enabled) {
    sources.push(
      createBoardSource({
        key: 'handshake',
        label: 'Handshake',
        listUrl: 'https://app.joinhandshake.com/stu/postings',
        origin: 'https://app.joinhandshake.com',
        hydrateMs: 3000,
      }),
    );
  }

  return sources;
}

export function sourceCap(key: string): number {
  const config = loadConfig({ requireEnv: false });
  const entry = (config.sources as Record<string, { cap: number } | undefined>)[key];
  return entry?.cap ?? 0;
}

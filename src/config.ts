import { readFileSync, existsSync } from 'node:fs';
import { resolve, isAbsolute } from 'node:path';
import { config as loadDotenv } from 'dotenv';
import { parse as parseYaml } from 'yaml';
import { z } from 'zod';

loadDotenv({ quiet: true });

export const PROJECT_ROOT = resolve(import.meta.dirname, '..');

/** Resolve a possibly-relative configured path against the project root. */
export function projectPath(p: string): string {
  return isAbsolute(p) ? p : resolve(PROJECT_ROOT, p);
}

const timeOfDay = z
  .string()
  .regex(/^([01]\d|2[0-3]):[0-5]\d$/, 'expected HH:MM in 24-hour form');

const sourceSchema = z.object({
  enabled: z.boolean(),
  cap: z.number().int().min(0),
});

const fileConfigSchema = z.object({
  daily: z.object({
    target: z.number().int().min(1).max(500),
    activeWindow: z.object({ start: timeOfDay, end: timeOfDay }),
    gapSeconds: z.object({
      min: z.number().int().min(0),
      max: z.number().int().min(0),
    }),
  }),
  sources: z.object({
    jobright: sourceSchema.extend({ listUrl: z.string().url() }),
    linkedin: sourceSchema.extend({ onlyEasyApply: z.boolean().default(false) }),
    wellfound: sourceSchema,
    handshake: sourceSchema,
  }),
  safety: z.object({
    circuitBreakerFailures: z.number().int().min(1),
    maxRepairLoops: z.number().int().min(0).max(10),
    maxFormSteps: z.number().int().min(1).max(30),
    humanWaitSeconds: z.number().int().min(0),
    requireAllRequiredFieldsFilled: z.boolean(),
  }),
  timeouts: z.object({
    navigation: z.number().int().min(1000),
    autofillSettle: z.number().int().min(1000),
    emailVerification: z.number().int().min(1000),
    elementWait: z.number().int().min(1000),
  }),
  assets: z.object({
    resumePath: z.string(),
    coverLetterPath: z.string().default(''),
  }),
});

const envSchema = z.object({
  OPENAI_API_KEY: z.string({ error: 'not set. Add your OpenAI key to .env' }).min(1, 'is empty'),
  OPENAI_MODEL: z.string().min(1).default('gpt-4.1'),
  CHROME_PROFILE_DIR: z.string().min(1).default('./.browser-profile'),
  CHROME_DEBUG_PORT: z.coerce.number().int().min(1024).max(65535).default(9222),
  CHROME_PATH: z.string().optional(),
  APPLICATION_EMAIL: z
    .string({ error: 'not set. Add the mailbox the agent should use for ATS accounts to .env' })
    .email('must be a valid email address'),
  WEBMAIL_PROVIDER: z.enum(['gmail', 'outlook']).default('gmail'),
  VAULT_KEY: z.string().optional(),
  NOTIFY_WEBHOOK: z.string().optional(),
  // Redirect all runtime state (database, screenshots, exports). Exists so tests
  // cannot write into a real run's application history.
  AGENT_DATA_DIR: z.string().optional(),
});

export const runModes = ['dry-run', 'review', 'auto'] as const;
export type RunMode = (typeof runModes)[number];

export type FileConfig = z.infer<typeof fileConfigSchema>;
export type Env = z.infer<typeof envSchema>;

export interface AppConfig extends FileConfig {
  env: Env;
  paths: {
    profileDir: string;
    dataDir: string;
    artifactsDir: string;
    exportsDir: string;
    dbFile: string;
    resume: string;
    coverLetter: string;
  };
}

let cached: AppConfig | null = null;

/**
 * Load and validate configuration. Env vars are only required for commands that
 * actually need them, so `requireEnv: false` lets read-only commands such as
 * `status` and `export` run without an OpenAI key present.
 */
export function loadConfig(options: { requireEnv?: boolean } = {}): AppConfig {
  if (cached) return cached;
  const { requireEnv = true } = options;

  const configPath = resolve(PROJECT_ROOT, 'config/config.yaml');
  if (!existsSync(configPath)) {
    throw new Error(`Missing config file at ${configPath}`);
  }

  const parsedFile = fileConfigSchema.safeParse(parseYaml(readFileSync(configPath, 'utf8')));
  if (!parsedFile.success) {
    throw new Error(`Invalid config/config.yaml:\n${formatIssues(parsedFile.error)}`);
  }
  const file = parsedFile.data;

  if (file.daily.gapSeconds.min > file.daily.gapSeconds.max) {
    throw new Error('config/config.yaml: daily.gapSeconds.min must not exceed max');
  }

  const envSource = requireEnv ? envSchema : envSchema.partial({
    OPENAI_API_KEY: true,
    APPLICATION_EMAIL: true,
  });
  const parsedEnv = envSource.safeParse(process.env);
  if (!parsedEnv.success) {
    throw new Error(
      `Invalid environment (copy .env.example to .env and fill it in):\n${formatIssues(parsedEnv.error)}`,
    );
  }
  const env = parsedEnv.data as Env;

  const dataDir = env.AGENT_DATA_DIR ? projectPath(env.AGENT_DATA_DIR) : resolve(PROJECT_ROOT, 'data');
  cached = {
    ...file,
    env,
    paths: {
      profileDir: projectPath(env.CHROME_PROFILE_DIR),
      dataDir,
      artifactsDir: resolve(dataDir, 'artifacts'),
      exportsDir: resolve(dataDir, 'exports'),
      dbFile: resolve(dataDir, 'agent.db'),
      resume: projectPath(file.assets.resumePath),
      coverLetter: file.assets.coverLetterPath ? projectPath(file.assets.coverLetterPath) : '',
    },
  };
  return cached;
}

/** Test seam: drop the memoised config so a fresh load re-reads disk and env. */
export function resetConfigCache(): void {
  cached = null;
}

function formatIssues(error: z.ZodError): string {
  return error.issues
    .map((issue) => `  - ${issue.path.join('.') || '(root)'}: ${issue.message}`)
    .join('\n');
}

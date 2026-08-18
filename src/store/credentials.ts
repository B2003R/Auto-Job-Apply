import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';
import { loadConfig } from '../config.ts';
import { getDb } from './db.ts';

const ALGORITHM = 'aes-256-gcm';
const IV_LENGTH = 12;

export interface StoredCredential {
  domain: string;
  email: string;
  password: string;
  createdAt: string;
  verifiedAt: string | null;
}

function vaultKey(): Buffer {
  const raw = loadConfig({ requireEnv: false }).env.VAULT_KEY;
  if (!raw) {
    throw new Error(
      'VAULT_KEY is not set. The agent creates ATS accounts and must store their passwords encrypted. ' +
        'Generate one with: openssl rand -base64 32',
    );
  }

  const key = Buffer.from(raw, 'base64');
  if (key.length !== 32) {
    throw new Error(`VAULT_KEY must decode to 32 bytes, got ${key.length}. Generate one with: openssl rand -base64 32`);
  }
  return key;
}

/**
 * AES-256-GCM with a per-record IV. GCM is chosen over CBC because it
 * authenticates the ciphertext, so a tampered or corrupted record fails loudly
 * on read instead of decrypting to garbage that gets typed into a login form.
 */
export function encryptSecret(plaintext: string): string {
  const iv = randomBytes(IV_LENGTH);
  const cipher = createCipheriv(ALGORITHM, vaultKey(), iv);
  const ciphertext = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return [iv.toString('base64'), tag.toString('base64'), ciphertext.toString('base64')].join(':');
}

export function decryptSecret(payload: string): string {
  const [ivPart, tagPart, dataPart] = payload.split(':');
  if (!ivPart || !tagPart || !dataPart) {
    throw new Error('Stored credential is malformed');
  }

  const decipher = createDecipheriv(ALGORITHM, vaultKey(), Buffer.from(ivPart, 'base64'));
  decipher.setAuthTag(Buffer.from(tagPart, 'base64'));
  return Buffer.concat([decipher.update(Buffer.from(dataPart, 'base64')), decipher.final()]).toString('utf8');
}

/**
 * Generate a password that satisfies the widest plausible ATS policy: mixed case,
 * digits, and a symbol drawn from a conservative set, because some validators
 * reject characters outside it.
 */
export function generatePassword(length = 20): string {
  const lower = 'abcdefghijkmnopqrstuvwxyz';
  const upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
  const digits = '23456789';
  const symbols = '!@#$%*-_';
  const all = lower + upper + digits + symbols;

  const pick = (set: string): string => set[randomBytes(1)[0]! % set.length]!;

  // Guarantee one of each class, then fill and shuffle so position is not predictable.
  const chars = [pick(lower), pick(upper), pick(digits), pick(symbols)];
  while (chars.length < length) chars.push(pick(all));

  for (let i = chars.length - 1; i > 0; i -= 1) {
    const j = randomBytes(1)[0]! % (i + 1);
    [chars[i], chars[j]] = [chars[j]!, chars[i]!];
  }
  return chars.join('');
}

export function saveCredential(domain: string, email: string, password: string, notes?: string): void {
  getDb()
    .prepare(
      `INSERT INTO credentials (domain, email, password_cipher, created_at, notes)
       VALUES (?, ?, ?, ?, ?)
       ON CONFLICT (domain, email) DO UPDATE SET password_cipher = excluded.password_cipher, notes = excluded.notes`,
    )
    .run(domain, email, encryptSecret(password), new Date().toISOString(), notes ?? null);
}

export function markCredentialVerified(domain: string, email: string): void {
  getDb()
    .prepare(`UPDATE credentials SET verified_at = ? WHERE domain = ? AND email = ?`)
    .run(new Date().toISOString(), domain, email);
}

/** Look up a previously created account so a repeat visit signs in instead of re-registering. */
export function findCredential(domain: string, email: string): StoredCredential | null {
  const row = getDb()
    .prepare(`SELECT domain, email, password_cipher, created_at, verified_at FROM credentials WHERE domain = ? AND email = ?`)
    .get(domain, email) as
    | { domain: string; email: string; password_cipher: string; created_at: string; verified_at: string | null }
    | undefined;

  if (!row) return null;
  return {
    domain: row.domain,
    email: row.email,
    password: decryptSecret(row.password_cipher),
    createdAt: row.created_at,
    verifiedAt: row.verified_at,
  };
}

export function listCredentials(): StoredCredential[] {
  const rows = getDb()
    .prepare(`SELECT domain, email, password_cipher, created_at, verified_at FROM credentials ORDER BY domain`)
    .all() as Array<{
    domain: string;
    email: string;
    password_cipher: string;
    created_at: string;
    verified_at: string | null;
  }>;

  return rows.map((row) => ({
    domain: row.domain,
    email: row.email,
    password: decryptSecret(row.password_cipher),
    createdAt: row.created_at,
    verifiedAt: row.verified_at,
  }));
}

/** Registrable domain, so accounts are keyed per vendor rather than per subdomain. */
export function credentialDomain(url: string): string {
  try {
    const host = new URL(url).hostname.toLowerCase();
    const parts = host.split('.');
    return parts.length > 2 ? parts.slice(-2).join('.') : host;
  } catch {
    return url;
  }
}

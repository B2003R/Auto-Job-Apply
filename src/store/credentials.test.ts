import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';

const dataDir = mkdtempSync(join(tmpdir(), 'vault-test-'));
process.env.OPENAI_API_KEY ??= 'test-key';
process.env.APPLICATION_EMAIL ??= 'ada@example.com';
process.env.VAULT_KEY = randomBytes(32).toString('base64');
// Keep the suite out of a real run's database.
process.env.AGENT_DATA_DIR = dataDir;

const { closeDb } = await import('./db.ts');
const {
  credentialDomain,
  decryptSecret,
  encryptSecret,
  findCredential,
  generatePassword,
  listCredentials,
  markCredentialVerified,
  saveCredential,
} = await import('./credentials.ts');

before(() => {
  // Touch the DB so the schema is applied before the first query.
  saveCredential('warmup.example', 'a@b.c', 'seed');
});

after(() => {
  closeDb();
  rmSync(dataDir, { recursive: true, force: true });
});

test('encryption round-trips', () => {
  const secret = 'correct horse battery staple';
  assert.equal(decryptSecret(encryptSecret(secret)), secret);
});

test('the same plaintext encrypts differently each time', () => {
  // A fixed IV would let identical passwords be spotted by comparing ciphertext.
  assert.notEqual(encryptSecret('same'), encryptSecret('same'));
});

test('a tampered record fails loudly instead of decrypting to garbage', () => {
  const payload = encryptSecret('sensitive');
  const [iv, tag, data] = payload.split(':');
  const flipped = Buffer.from(data!, 'base64');
  flipped[0] = flipped[0]! ^ 0xff;
  const tampered = [iv, tag, flipped.toString('base64')].join(':');

  assert.throws(() => decryptSecret(tampered));
});

test('rejects a malformed record rather than returning a partial value', () => {
  assert.throws(() => decryptSecret('not-a-valid-payload'), /malformed/);
});

test('generated passwords satisfy a broad ATS policy', () => {
  for (let i = 0; i < 50; i += 1) {
    const password = generatePassword();
    assert.equal(password.length, 20);
    assert.match(password, /[a-z]/, 'needs a lowercase letter');
    assert.match(password, /[A-Z]/, 'needs an uppercase letter');
    assert.match(password, /[0-9]/, 'needs a digit');
    assert.match(password, /[!@#$%*\-_]/, 'needs a symbol');
    assert.doesNotMatch(password, /\s/, 'must not contain whitespace');
  }
});

test('generated passwords are unique', () => {
  const seen = new Set(Array.from({ length: 200 }, () => generatePassword()));
  assert.equal(seen.size, 200);
});

test('stores and retrieves a credential', () => {
  saveCredential('greenhouse.io', 'ada@example.com', 'Sup3rSecret!');
  const found = findCredential('greenhouse.io', 'ada@example.com');
  assert.equal(found?.password, 'Sup3rSecret!');
  assert.equal(found?.verifiedAt, null);
});

test('re-saving updates the password rather than creating a duplicate', () => {
  saveCredential('lever.co', 'ada@example.com', 'first');
  saveCredential('lever.co', 'ada@example.com', 'second');
  assert.equal(findCredential('lever.co', 'ada@example.com')?.password, 'second');
  assert.equal(listCredentials().filter((c) => c.domain === 'lever.co').length, 1);
});

test('records verification', () => {
  saveCredential('ashbyhq.com', 'ada@example.com', 'x');
  markCredentialVerified('ashbyhq.com', 'ada@example.com');
  assert.ok(findCredential('ashbyhq.com', 'ada@example.com')?.verifiedAt);
});

test('returns null for an account that was never created', () => {
  assert.equal(findCredential('unknown.example', 'ada@example.com'), null);
});

test('keys accounts per vendor, not per subdomain', () => {
  // The same ATS serves many companies on different subdomains; one account
  // covers them all, so re-registering per subdomain would fail on a duplicate.
  assert.equal(credentialDomain('https://boards.greenhouse.io/acme/jobs/1'), 'greenhouse.io');
  assert.equal(credentialDomain('https://acme.wd1.myworkdayjobs.com/x'), 'myworkdayjobs.com');
  assert.equal(credentialDomain('https://jobs.lever.co/acme'), 'lever.co');
  assert.equal(credentialDomain('nonsense'), 'nonsense');
});

import { listCredentials } from '../store/credentials.ts';

/**
 * Print the ATS accounts the agent created, with passwords in the clear.
 *
 * Decryption is explicit and on demand rather than part of any routine output, so
 * plaintext passwords only ever appear when the operator asks for them.
 */
export function vaultExport(): void {
  const credentials = listCredentials();

  if (credentials.length === 0) {
    process.stdout.write('\nNo ATS accounts have been created yet.\n\n');
    return;
  }

  process.stdout.write(`\n${credentials.length} ATS account(s) created by the agent:\n\n`);
  for (const credential of credentials) {
    process.stdout.write(
      [
        `  ${credential.domain}`,
        `    email     ${credential.email}`,
        `    password  ${credential.password}`,
        `    created   ${credential.createdAt}`,
        `    verified  ${credential.verifiedAt ?? 'not verified'}`,
        '',
      ].join('\n'),
    );
  }
  process.stdout.write('Store these in your password manager; they are only recoverable with VAULT_KEY.\n\n');
}

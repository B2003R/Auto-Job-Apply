import type { BrowserContext, Page } from 'playwright';
import { loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { pause, typeLikeHuman, waitForDomToSettle } from '../browser/humanize.ts';
import { completeVerificationLink, findVerificationMessage } from '../mail/webmail.ts';
import {
  credentialDomain,
  findCredential,
  generatePassword,
  markCredentialVerified,
  saveCredential,
} from '../store/credentials.ts';
import { extractFormSnapshot, type FormField, type FormSnapshot } from './formExtractor.ts';
import { loadAnswerBank, lookupAnswer } from './answerBank.ts';
import { classifyPage } from './pageClassifier.ts';

export type AccountOutcome =
  | { status: 'ready'; detail: string }
  | { status: 'needs_human'; detail: string }
  | { status: 'failed'; detail: string };

/**
 * Get past an account wall so the application can proceed.
 *
 * Reuses a stored account for the vendor when one exists, since the agent will
 * hit the same ATS many times and re-registering would fail on a duplicate
 * email. Only registers when there is nothing to sign in with.
 */
export async function handleAccountWall(
  page: Page,
  context: BrowserContext,
  kind: 'login_required' | 'signup_required' | 'email_verification_required',
): Promise<AccountOutcome> {
  const config = loadConfig();
  const email = config.env.APPLICATION_EMAIL;
  const domain = credentialDomain(page.url());
  const existing = findCredential(domain, email);

  if (kind === 'email_verification_required') {
    return verifyEmail(page, context, domain, email);
  }

  if (kind === 'login_required') {
    if (!existing) {
      return {
        status: 'needs_human',
        detail: `${domain} wants an existing account but none is stored. Sign in once in the agent's Chrome profile, or let it register on a signup page.`,
      };
    }
    return signIn(page, email, existing.password, domain);
  }

  // signup_required
  if (existing) {
    logger.info('Account already exists for this vendor; signing in instead of registering', { domain });
    const signedIn = await signIn(page, email, existing.password, domain);
    if (signedIn.status === 'ready') return signedIn;
  }

  return register(page, context, domain, email);
}

async function signIn(page: Page, email: string, password: string, domain: string): Promise<AccountOutcome> {
  const snapshot = await extractFormSnapshot(page);
  const emailField = findField(snapshot, [/e-?mail/i, /username/i]);
  const passwordField = findField(snapshot, [/password/i]);

  if (!emailField || !passwordField) {
    return { status: 'needs_human', detail: `Could not find sign-in fields on ${domain}` };
  }

  await fill(page, emailField, email);
  await fill(page, passwordField, password);
  await pause();

  const submit = snapshot.buttons.find((b) => b.kind === 'submit' || /sign in|log in|continue/i.test(b.label));
  if (!submit) return { status: 'needs_human', detail: `No sign-in button found on ${domain}` };

  await clickButton(page, submit.id);
  await waitForDomToSettle(page, 1200, 15_000);

  const after = await classifyPage(page).catch(() => null);
  if (after && (after.kind === 'login_required' || after.kind === 'signup_required')) {
    return { status: 'needs_human', detail: `Sign-in to ${domain} did not take effect; the credential may be stale` };
  }
  if (after?.kind === 'captcha') {
    return { status: 'needs_human', detail: `${domain} presented a challenge during sign-in` };
  }

  logger.info('Signed in to ATS', { domain });
  return { status: 'ready', detail: `Signed in to ${domain}` };
}

async function register(
  page: Page,
  context: BrowserContext,
  domain: string,
  email: string,
): Promise<AccountOutcome> {
  const snapshot = await extractFormSnapshot(page);
  const bank = loadAnswerBank();

  const emailField = findField(snapshot, [/e-?mail/i, /username/i]);
  const passwordFields = snapshot.fields.filter((f) => /password/i.test(f.label) && !f.disabled);

  if (!emailField || passwordFields.length === 0) {
    return { status: 'needs_human', detail: `Could not find registration fields on ${domain}` };
  }

  const password = generatePassword();
  // Persist before submitting: if the request succeeds but the response is lost,
  // an unrecorded password would lock the operator out of a real account.
  saveCredential(domain, email, password, 'Created automatically during an application');

  await fill(page, emailField, email);
  for (const field of passwordFields) {
    await fill(page, field, password);
  }

  // Registration forms often also want a name; answer anything else we can.
  for (const field of snapshot.fields) {
    if (field.disabled || field.value !== '' || /password/i.test(field.label)) continue;
    if (field.id === emailField.id) continue;
    const answer = lookupAnswer(field.label, bank);
    if (answer) await fill(page, field, answer);
  }

  const signupStartedAt = new Date();
  const submit = snapshot.buttons.find(
    (b) => b.kind === 'submit' || /create|register|sign up|continue/i.test(b.label),
  );
  if (!submit) return { status: 'needs_human', detail: `No registration button found on ${domain}` };

  await pause();
  await clickButton(page, submit.id);
  await waitForDomToSettle(page, 1500, 20_000);

  const after = await classifyPage(page).catch(() => null);

  if (after?.kind === 'captcha') {
    return { status: 'needs_human', detail: `${domain} presented a challenge during registration` };
  }

  if (after?.kind === 'signup_required') {
    const errors = after.snapshot.errorBanners.join('; ');
    return {
      status: 'failed',
      detail: `Registration on ${domain} was rejected${errors ? `: ${errors}` : ''}`,
    };
  }

  logger.info('Registered ATS account', { domain, email });

  if (after?.kind === 'email_verification_required') {
    return verifyEmail(page, context, domain, email, signupStartedAt);
  }

  return { status: 'ready', detail: `Registered a new account on ${domain}` };
}

/**
 * Complete an email verification step by reading the operator's webmail.
 *
 * Handles both shapes ATS vendors use: a link to click, and a code to type back
 * into the page. The link is opened in a separate tab so the half-filled
 * application is not lost.
 */
async function verifyEmail(
  page: Page,
  context: BrowserContext,
  domain: string,
  email: string,
  since = new Date(Date.now() - 5 * 60_000),
): Promise<AccountOutcome> {
  const hit = await findVerificationMessage(context, { senderDomain: domain, since });

  if (!hit) {
    return {
      status: 'needs_human',
      detail: `No verification email from ${domain} arrived in time`,
    };
  }

  if (hit.link) {
    const opened = await completeVerificationLink(context, hit.link);
    if (!opened) {
      return { status: 'needs_human', detail: `Verification link from ${domain} did not load` };
    }

    await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => undefined);
    await waitForDomToSettle(page, 1000, 12_000);
    markCredentialVerified(domain, email);
    return { status: 'ready', detail: `Verified the ${domain} account via emailed link` };
  }

  if (hit.code) {
    const snapshot = await extractFormSnapshot(page);
    const codeField = findField(snapshot, [/code/i, /verification/i, /otp/i, /pin/i]);
    if (!codeField) {
      return { status: 'needs_human', detail: `Received code ${hit.code} but found no field to enter it on ${domain}` };
    }

    await fill(page, codeField, hit.code);
    const submit = snapshot.buttons.find((b) => b.kind === 'submit' || /verify|confirm|continue/i.test(b.label));
    if (submit) {
      await clickButton(page, submit.id);
      await waitForDomToSettle(page, 1200, 15_000);
    }

    markCredentialVerified(domain, email);
    return { status: 'ready', detail: `Verified the ${domain} account with an emailed code` };
  }

  return { status: 'needs_human', detail: `Verification email from ${domain} contained neither a link nor a code` };
}

function findField(snapshot: FormSnapshot, patterns: RegExp[]): FormField | undefined {
  for (const pattern of patterns) {
    const match = snapshot.fields.find((f) => !f.disabled && pattern.test(f.label));
    if (match) return match;
  }
  return undefined;
}

async function fill(page: Page, field: FormField, value: string): Promise<void> {
  const locator = page.locator(`[data-agent-field="${field.id}"]`).first();
  if (field.kind === 'checkbox') {
    await locator.check().catch(() => undefined);
    return;
  }
  await typeLikeHuman(locator, value).catch(() => undefined);
}

async function clickButton(page: Page, buttonId: string): Promise<void> {
  await page.locator(`[data-agent-button="${buttonId}"]`).first().click({ timeout: 12_000 });
}

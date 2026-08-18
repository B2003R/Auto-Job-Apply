import type { Page } from 'playwright';
import { logger } from '../logger.ts';
import { requestStructured } from '../llm/client.ts';
import {
  CLASSIFIER_SYSTEM_PROMPT,
  CONFIRMATION_SYSTEM_PROMPT,
  buildClassifierUserMessage,
} from '../llm/prompts.ts';
import { classificationJsonSchema, classificationSchema, type Classification, type PageKind } from '../llm/schemas.ts';
import { extractFormSnapshot, type FormSnapshot } from './formExtractor.ts';

/** Known ATS vendors, identified from the host. Used for logging and per-vendor quirks. */
export function detectAtsVendor(url: string): string | null {
  const host = safeHost(url);
  if (!host) return null;

  const vendors: Array<[RegExp, string]> = [
    [/myworkdayjobs\.com|workday(site)?\.com|wd\d+\.myworkday/, 'workday'],
    [/greenhouse\.io|boards\.greenhouse/, 'greenhouse'],
    [/lever\.co/, 'lever'],
    [/ashbyhq\.com/, 'ashby'],
    [/icims\.com/, 'icims'],
    [/taleo\.net/, 'taleo'],
    [/smartrecruiters\.com/, 'smartrecruiters'],
    [/workable\.com/, 'workable'],
    [/bamboohr\.com/, 'bamboohr'],
    [/jobvite\.com/, 'jobvite'],
    [/successfactors\.(com|eu)/, 'successfactors'],
    [/oraclecloud\.com/, 'oracle'],
    [/paylocity\.com/, 'paylocity'],
    [/breezy\.hr/, 'breezy'],
    [/teamtailor\.com/, 'teamtailor'],
    [/linkedin\.com/, 'linkedin'],
    [/wellfound\.com|angel\.co/, 'wellfound'],
    [/joinhandshake\.com/, 'handshake'],
  ];

  for (const [pattern, name] of vendors) {
    if (pattern.test(host)) return name;
  }
  return null;
}

export interface PageAssessment extends Classification {
  /** How the verdict was reached, so low-confidence LLM guesses are traceable. */
  via: 'heuristic' | 'model';
  snapshot: FormSnapshot;
  atsVendor: string | null;
}

/**
 * Work out what kind of page the agent is looking at.
 *
 * Heuristics run first and answer the unambiguous cases for free. The model is
 * only consulted when the signals are weak, which keeps cost and latency
 * proportional to how confusing the page actually is.
 */
export async function classifyPage(page: Page): Promise<PageAssessment> {
  const snapshot = await extractFormSnapshot(page);
  const atsVendor = detectAtsVendor(page.url());
  const visibleText = await readVisibleText(page);

  const heuristic = classifyByHeuristics(page.url(), snapshot, visibleText);
  if (heuristic) {
    logger.debug('Page classified by heuristics', { kind: heuristic.kind, evidence: heuristic.evidence });
    return { ...heuristic, via: 'heuristic', snapshot, atsVendor };
  }

  const classification = await requestStructured({
    purpose: 'classify-page',
    system: CLASSIFIER_SYSTEM_PROMPT,
    user: buildClassifierUserMessage(snapshot, visibleText),
    schemaName: 'page_classification',
    jsonSchema: classificationJsonSchema as unknown as Record<string, unknown>,
    validator: classificationSchema,
  });

  logger.debug('Page classified by model', {
    kind: classification.kind,
    confidence: classification.confidence,
  });
  return { ...classification, via: 'model', snapshot, atsVendor };
}

/**
 * Cheap, high-confidence classification from unambiguous signals.
 *
 * Returns null when nothing decisive is present, deliberately deferring rather
 * than committing to a weak guess: a misclassification here sends the whole
 * application down the wrong branch.
 */
export function classifyByHeuristics(
  url: string,
  snapshot: FormSnapshot,
  visibleText: string,
): Classification | null {
  const text = visibleText.toLowerCase();
  const haystack = `${text} ${snapshot.title.toLowerCase()} ${snapshot.headings.join(' ').toLowerCase()}`;

  // CAPTCHA first: it invalidates every other reading of the page.
  const captchaMarkers = [
    'recaptcha',
    'hcaptcha',
    'are you a robot',
    'verify you are human',
    'unusual traffic',
    'cloudflare',
    'checking your browser',
    'press and hold',
  ];
  const captchaHit = captchaMarkers.find((m) => haystack.includes(m));
  if (captchaHit) {
    return { kind: 'captcha', confidence: 0.9, evidence: `Page mentions "${captchaHit}"` };
  }

  const expiredMarkers = [
    'no longer accepting applications',
    'this job is no longer available',
    'position has been filled',
    'posting has expired',
    'job not found',
    'this position is closed',
  ];
  const expiredHit = expiredMarkers.find((m) => haystack.includes(m));
  if (expiredHit) {
    return { kind: 'expired_or_closed', confidence: 0.9, evidence: `Page states "${expiredHit}"` };
  }

  const confirmationMarkers = [
    'application submitted',
    'application received',
    'thank you for applying',
    'thanks for applying',
    'your application has been submitted',
    'we have received your application',
    'application complete',
    'successfully submitted',
  ];
  const confirmationHit = confirmationMarkers.find((m) => haystack.includes(m));
  // A form still on the page contradicts a confirmation, so require both signals.
  if (confirmationHit && snapshot.fields.filter((f) => !f.disabled).length <= 2) {
    return { kind: 'confirmation', confidence: 0.85, evidence: `Page states "${confirmationHit}"` };
  }

  const emailVerificationMarkers = [
    'verify your email',
    'confirm your email',
    'check your email',
    'verification code',
    'we sent you a code',
    'we have sent a verification',
  ];
  const emailHit = emailVerificationMarkers.find((m) => haystack.includes(m));
  if (emailHit) {
    return { kind: 'email_verification_required', confidence: 0.8, evidence: `Page states "${emailHit}"` };
  }

  const hasPassword = snapshot.fields.some((f) => /password/i.test(f.label));
  const hasConfirmPassword = snapshot.fields.filter((f) => /password/i.test(f.label)).length > 1;
  const signupWords = ['create an account', 'create account', 'sign up', 'register', 'new account'];
  const signupHit = signupWords.find((w) => haystack.includes(w));

  if (hasPassword && (hasConfirmPassword || signupHit)) {
    return {
      kind: 'signup_required',
      confidence: 0.8,
      evidence: hasConfirmPassword
        ? 'Form has two password fields'
        : `Password field alongside "${signupHit}"`,
    };
  }

  if (hasPassword) {
    const loginWords = ['sign in', 'log in', 'login', 'welcome back'];
    const loginHit = loginWords.find((w) => haystack.includes(w));
    if (loginHit) {
      return { kind: 'login_required', confidence: 0.8, evidence: `Password field alongside "${loginHit}"` };
    }
  }

  // A substantial form with a submit control is almost certainly the application.
  const meaningfulFields = snapshot.fields.filter((f) => !f.disabled && f.kind !== 'unknown');
  const hasSubmit = snapshot.buttons.some((b) => b.kind === 'submit' || b.kind === 'next');
  if (meaningfulFields.length >= 5 && hasSubmit && !hasPassword) {
    return {
      kind: 'application_form',
      confidence: 0.75,
      evidence: `${meaningfulFields.length} fields with a submit or continue control`,
    };
  }

  return null;
}

/**
 * Confirm a submission actually succeeded.
 *
 * Held to a higher bar than ordinary classification, and always asks the model
 * even when heuristics matched, because a false positive means the operator
 * believes they applied when they did not. That error is silent and permanent,
 * whereas a false negative just leaves a URL in the day's export to check.
 */
export async function confirmSubmission(page: Page): Promise<{ confirmed: boolean; evidence: string; confidence: number }> {
  const snapshot = await extractFormSnapshot(page);
  const visibleText = await readVisibleText(page);

  const classification = await requestStructured({
    purpose: 'confirm-submission',
    system: CONFIRMATION_SYSTEM_PROMPT,
    user: buildClassifierUserMessage(snapshot, visibleText),
    schemaName: 'page_classification',
    jsonSchema: classificationJsonSchema as unknown as Record<string, unknown>,
    validator: classificationSchema,
  }).catch((error: unknown) => {
    logger.warn('Confirmation check failed', { error: error instanceof Error ? error.message : String(error) });
    return null;
  });

  if (!classification) {
    return { confirmed: false, evidence: 'Confirmation check could not be completed', confidence: 0 };
  }

  const confirmed = classification.kind === 'confirmation' && classification.confidence >= 0.7;
  return { confirmed, evidence: classification.evidence, confidence: classification.confidence };
}

export async function readVisibleText(page: Page): Promise<string> {
  return page
    .evaluate(() => (document.body?.innerText ?? '').replace(/\n{3,}/g, '\n\n').trim())
    .catch(() => '');
}

/** Page kinds that mean the application cannot proceed without a person. */
export const HUMAN_REQUIRED_KINDS: PageKind[] = ['captcha'];

function safeHost(url: string): string | null {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return null;
  }
}

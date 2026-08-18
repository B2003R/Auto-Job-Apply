import type { FormSnapshot } from '../apply/formExtractor.ts';
import type { AutofillResult } from '../jobright/autofill.ts';

/**
 * The verifier's instructions.
 *
 * The central rule is that the model may not invent facts about the applicant.
 * An application is a factual document submitted in the operator's name, so a
 * plausible-but-wrong answer is worse than a blocked application: the operator
 * can fix a blocked one, but never learns about a fabricated one. Hence
 * "escalate rather than guess" is stated as the priority, and the model is given
 * an explicit channel (`unanswerable`) for doing so.
 */
export const VERIFIER_SYSTEM_PROMPT = `
You verify job application forms that have just been auto-filled, and correct
them before a separate system decides whether to submit.

THE ONE RULE THAT OVERRIDES EVERYTHING ELSE
Never invent information about the applicant. Every value you supply must be
copied from the APPLICANT DATA block, or be a direct restatement of it (for
example reformatting a phone number, or splitting a full name). If a required
field asks something the APPLICANT DATA does not answer, do NOT guess: list it
in "unanswerable" and set status to "needs_human". A wrong answer in a submitted
application cannot be taken back, whereas a blocked application is simply
finished by the operator.

WHAT TO DO
1. Compare every field in the FORM SNAPSHOT against the APPLICANT DATA.
2. Propose fixes for fields that are empty-but-required, clearly wrong, or
   showing a validation error.
3. Judge whether the form is ready.

STATUS MEANINGS
- "ready_to_submit": every required field on this page holds a correct value and
  no validation errors remain. This page is the final step of the application.
- "needs_fixes": you have supplied fixes that should resolve the outstanding
  problems. The fixes will be applied and the page re-checked.
- "needs_next_step": this page is complete but it is not the last one. Include a
  single click_button fix targeting the next/continue button.
- "needs_human": something needs a person. Use this for CAPTCHAs, logins,
  unanswerable required questions, or anything you are not confident about.
- "not_an_application_form": the page is not an application form at all (an
  error page, an expired posting, a job description with no form).

FIXES
- Address fields only by the "id" given in the snapshot. Never invent an id.
- For select_option and choose_radio, "value" must exactly match one of the
  strings in that field's "options" list.
- Use upload_resume for a file input that wants a resume or CV.
- Only use click_button for a next/continue button, and only with
  "needs_next_step". You cannot submit the application; that decision is not
  yours.
- Give each fix a short "reason" naming which piece of applicant data it came
  from.

CHOOSING VALUES
- Prefer the most specific applicable answer. If asked for years of experience
  with a named technology, use skillYears for that technology rather than the
  overall total.
- For voluntary EEO and demographic questions, use the configured answers, which
  are usually a decline-to-answer option. If the exact configured wording is not
  among the options, pick the option closest in meaning to declining.
- Leave optional fields empty when the applicant data does not cover them. Only
  required fields are worth blocking on.
- Never change a field that already holds a correct value.
`.trim();

export const CLASSIFIER_SYSTEM_PROMPT = `
You identify what kind of page a job-application automation has landed on.

Answer with one "kind":
- "application_form": a form to fill in and submit for a job.
- "login_required": an existing account must be signed into before applying.
- "signup_required": a new account must be created before applying.
- "email_verification_required": an account exists but the email must be
  confirmed, or a code from an email must be entered.
- "captcha": a CAPTCHA, human-verification challenge, or bot wall.
- "confirmation": the application has been received or submitted successfully.
- "expired_or_closed": the posting is gone, closed, or no longer accepting
  applications.
- "external_redirect": an interstitial that will send the user somewhere else to
  apply, with no form of its own.
- "other": none of the above.

Set "confidence" between 0 and 1, and quote the specific on-page text you relied
on in "evidence". Prefer a low confidence over a confident guess; a caller
treats low confidence as a reason to involve a human.
`.trim();

export const CONFIRMATION_SYSTEM_PROMPT = `
You determine whether a job application was actually submitted successfully.

Be strict and sceptical. Answer "confirmation" only when the page states the
application was received, submitted, or is complete. A form that merely looks
empty, a "thank you for your interest" marketing page, or a page that still
shows the form are all NOT confirmations. A false positive means the operator
believes they applied when they did not, so when the evidence is ambiguous
choose "other" with low confidence.
`.trim();

export interface VerifierInput {
  snapshot: FormSnapshot;
  autofill: AutofillResult;
  applicantData: string;
  /** Deterministic answers already resolved from the bank, per field id. */
  resolvedAnswers: Array<{ fieldId: string; label: string; answer: string }>;
  jobContext: { company?: string | null; title?: string | null; url: string };
  attempt: number;
  maxAttempts: number;
  resumeAvailable: boolean;
}

export function buildVerifierUserMessage(input: VerifierInput): string {
  const { snapshot, autofill, resolvedAnswers, jobContext, attempt, maxAttempts, resumeAvailable } = input;

  const autofillSummary = autofill.triggered
    ? `The Jobright extension ran.${
        autofill.filled !== null && autofill.required !== null
          ? ` It reports ${autofill.filled} of ${autofill.required} required fields filled.`
          : ' It gave no progress readout.'
      }${autofill.blockedReason ? ` It also reported: "${autofill.blockedReason}".` : ''}`
    : 'The Jobright extension did not run on this page, so any values present were already there. You may need to supply most fields yourself.';

  const sections = [
    `JOB: ${jobContext.title ?? 'unknown title'} at ${jobContext.company ?? 'unknown company'}`,
    `PAGE URL: ${jobContext.url}`,
    `PAGE TITLE: ${snapshot.title}`,
    snapshot.headings.length > 0 ? `HEADINGS: ${snapshot.headings.join(' | ')}` : '',
    '',
    `AUTOFILL: ${autofillSummary}`,
    `ATTEMPT ${attempt} of ${maxAttempts}. On the final attempt, prefer "needs_human" over further fixes.`,
    `RESUME FILE AVAILABLE FOR UPLOAD: ${resumeAvailable ? 'yes' : 'no'}`,
    '',
    snapshot.errorBanners.length > 0
      ? `PAGE-LEVEL ERRORS:\n${snapshot.errorBanners.map((e) => `- ${e}`).join('\n')}`
      : 'PAGE-LEVEL ERRORS: none',
    '',
    'APPLICANT DATA (the only facts you may use):',
    input.applicantData,
    '',
    resolvedAnswers.length > 0
      ? `ANSWERS ALREADY RESOLVED FROM THE ANSWER BANK (treat as authoritative; do not contradict):\n${resolvedAnswers
          .map((r) => `- ${r.fieldId} (${r.label}): ${r.answer}`)
          .join('\n')}`
      : '',
    '',
    'FORM SNAPSHOT:',
    JSON.stringify({ fields: snapshot.fields, buttons: snapshot.buttons }, null, 2),
    '',
    'A screenshot of the page follows. Use it to spot rendered validation messages',
    'and required markers that the extracted snapshot may have missed.',
  ];

  return sections.filter((s) => s !== '').join('\n');
}

export function buildClassifierUserMessage(snapshot: FormSnapshot, visibleText: string): string {
  return [
    `URL: ${snapshot.url}`,
    `TITLE: ${snapshot.title}`,
    snapshot.headings.length > 0 ? `HEADINGS: ${snapshot.headings.join(' | ')}` : '',
    `FIELD COUNT: ${snapshot.fields.length}`,
    `FIELD LABELS: ${snapshot.fields.map((f) => f.label).filter(Boolean).slice(0, 40).join(' | ') || 'none'}`,
    `BUTTONS: ${snapshot.buttons.map((b) => b.label).slice(0, 20).join(' | ') || 'none'}`,
    snapshot.errorBanners.length > 0 ? `ALERTS: ${snapshot.errorBanners.join(' | ')}` : '',
    '',
    'VISIBLE TEXT (truncated):',
    visibleText.slice(0, 4000),
  ]
    .filter((s) => s !== '')
    .join('\n');
}

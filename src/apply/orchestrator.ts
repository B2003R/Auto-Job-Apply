import type { BrowserContext, Page } from 'playwright';
import { loadConfig, type RunMode } from '../config.ts';
import { logger } from '../logger.ts';
import { pause, scrollThroughPage, waitForDomToSettle } from '../browser/humanize.ts';
import { runJobrightAutofill } from '../jobright/autofill.ts';
import {
  bumpCounter,
  finishAttempt,
  recordArtifact,
  recordEvent,
  recordUnansweredQuestion,
  setJobStatus,
  startAttempt,
  type JobRow,
} from '../store/db.ts';
import type { ApplyClickResult, JobSource } from '../sources/types.ts';
import { handleAccountWall } from './account.ts';
import { escalateToHuman } from './escalate.ts';
import { extractFormSnapshot } from './formExtractor.ts';
import { classifyPage, type PageAssessment } from './pageClassifier.ts';
import { verifyAndRepair } from './repair.ts';
import { captureScreenshot, decideSubmission, submitApplication } from './submit.ts';
import { BUTTON_ATTR } from './formExtractor.ts';

export type FinalState =
  | 'submitted'
  | 'dry_run_complete'
  | 'blocked_human'
  | 'failed'
  | 'skipped';

export type FailureClass =
  | 'apply_unavailable'
  | 'posting_closed'
  | 'captcha'
  | 'account_required'
  | 'email_verification_failed'
  | 'form_incomplete'
  | 'unanswered_question'
  | 'submit_unconfirmed'
  | 'too_many_steps'
  | 'not_an_application'
  | 'error';

export interface ApplicationResult {
  jobId: string;
  finalState: FinalState;
  failureClass: FailureClass | null;
  detail: string;
  finalUrl: string;
  atsVendor: string | null;
  stepsCompleted: number;
  screenshotPath: string | null;
}

export interface RunOptions {
  mode: RunMode;
  /** False for unattended runs: escalations notify but do not block. */
  interactive: boolean;
}

/**
 * Drive one job from posting to outcome.
 *
 * Every meaningful transition is written to the events table before the next
 * action runs, so an interrupted run can be understood after the fact and a job
 * is never silently half-applied. The function always resolves with a result
 * rather than throwing: one bad posting must not end the day's run.
 */
export async function applyToJob(
  context: BrowserContext,
  source: JobSource,
  job: JobRow,
  options: RunOptions,
): Promise<ApplicationResult> {
  const config = loadConfig();
  const jobLabel = `${job.title ?? 'unknown role'} at ${job.company ?? 'unknown company'}`;
  const attemptId = startAttempt(job.id, options.mode);
  const log = logger.child({ job: job.id, source: source.key });

  bumpCounter(source.key, 'attempted');
  recordEvent('OPENING_JOB', { jobId: job.id, attemptId, message: job.url });

  let page: Page | null = null;
  let stepsCompleted = 0;
  let atsVendor: string | null = null;

  const finish = (
    finalState: FinalState,
    failureClass: FailureClass | null,
    detail: string,
    screenshotPath: string | null,
    finalUrl: string,
  ): ApplicationResult => {
    finishAttempt(attemptId, {
      finalState,
      submitted: finalState === 'submitted',
      atsVendor,
      finalUrl,
      failureClass,
      failureMessage: failureClass ? detail : null,
      stepsCompleted,
    });

    const jobStatus =
      finalState === 'submitted'
        ? 'submitted'
        : finalState === 'blocked_human'
          ? 'blocked_human'
          : finalState === 'skipped'
            ? 'skipped'
            : finalState === 'dry_run_complete'
              ? 'queued'
              : 'failed';
    setJobStatus(job.id, jobStatus, finalUrl);

    if (finalState === 'submitted') bumpCounter(source.key, 'submitted');
    recordEvent(finalState.toUpperCase(), { jobId: job.id, attemptId, message: detail });
    log.info('Application finished', { finalState, failureClass, detail });

    return {
      jobId: job.id,
      finalState,
      failureClass,
      detail,
      finalUrl,
      atsVendor,
      stepsCompleted,
      screenshotPath,
    };
  };

  try {
    const applyResult: ApplyClickResult = await source.clickApply(context, {
      source: job.source,
      url: job.url,
      company: job.company ?? undefined,
      title: job.title ?? undefined,
      externalId: job.external_id ?? undefined,
    });

    if (applyResult.kind === 'unavailable') {
      return finish('failed', 'apply_unavailable', applyResult.note, null, job.url);
    }

    page = applyResult.page;
    recordEvent('ON_ATS', { jobId: job.id, attemptId, message: page.url() });
    await scrollThroughPage(page, 2);

    // Walk the application, which may span several pages.
    for (let step = 1; step <= config.safety.maxFormSteps; step += 1) {
      await waitForDomToSettle(page, 800, 12_000);

      let assessment: PageAssessment = await classifyPage(page);
      atsVendor = assessment.atsVendor ?? atsVendor;
      recordEvent('CLASSIFIED', {
        jobId: job.id,
        attemptId,
        message: assessment.kind,
        data: { via: assessment.via, confidence: assessment.confidence, step },
      });

      if (assessment.kind === 'captcha') {
        const escalation = await escalateToHuman(page, {
          reason: 'A CAPTCHA or bot-verification challenge is blocking this application',
          jobLabel,
          url: page.url(),
          interactive: options.interactive,
        });
        if (!escalation.resolved) {
          const shot = await captureScreenshot(page, `${job.id}-captcha`);
          if (shot) recordArtifact('captcha', shot, { jobId: job.id, attemptId });
          return finish('blocked_human', 'captcha', escalation.detail, shot, page.url());
        }
        assessment = await classifyPage(page);
      }

      if (assessment.kind === 'expired_or_closed') {
        return finish('skipped', 'posting_closed', assessment.evidence, null, page.url());
      }

      if (assessment.kind === 'confirmation') {
        // Some flows land straight on a confirmation, for example a one-click apply.
        const shot = await captureScreenshot(page, `${job.id}-confirmed`);
        if (shot) recordArtifact('confirmation', shot, { jobId: job.id, attemptId });
        return finish('submitted', null, `Confirmed: ${assessment.evidence}`, shot, page.url());
      }

      if (
        assessment.kind === 'login_required' ||
        assessment.kind === 'signup_required' ||
        assessment.kind === 'email_verification_required'
      ) {
        recordEvent('ACCOUNT_WALL', { jobId: job.id, attemptId, message: assessment.kind });
        const outcome = await handleAccountWall(page, context, assessment.kind);
        recordEvent('ACCOUNT_RESULT', { jobId: job.id, attemptId, message: outcome.detail });

        if (outcome.status !== 'ready') {
          const failureClass: FailureClass =
            assessment.kind === 'email_verification_required' ? 'email_verification_failed' : 'account_required';
          const shot = await captureScreenshot(page, `${job.id}-account`);
          return finish(
            outcome.status === 'needs_human' ? 'blocked_human' : 'failed',
            failureClass,
            outcome.detail,
            shot,
            page.url(),
          );
        }
        continue;
      }

      if (assessment.kind === 'external_redirect' || assessment.kind === 'other') {
        const advanced = await followApplyAffordance(page);
        if (advanced) {
          stepsCompleted += 1;
          continue;
        }
        const shot = await captureScreenshot(page, `${job.id}-unclear`);
        return finish(
          'failed',
          'not_an_application',
          `Could not find an application form (${assessment.kind}: ${assessment.evidence})`,
          shot,
          page.url(),
        );
      }

      // assessment.kind === 'application_form'
      recordEvent('AUTOFILLING', { jobId: job.id, attemptId, data: { step } });
      const autofill = await runJobrightAutofill(page);
      recordEvent('AUTOFILLED', {
        jobId: job.id,
        attemptId,
        data: { triggered: autofill.triggered, filled: autofill.filled, required: autofill.required },
      });

      const repair = await verifyAndRepair(page, {
        company: job.company,
        title: job.title,
        autofill,
        onUnanswerable: (question, field) => {
          recordUnansweredQuestion({
            jobId: job.id,
            domain: atsVendor ?? undefined,
            question,
            fieldType: field.kind,
            options: field.options,
          });
        },
      });
      recordEvent('VERIFIED', {
        jobId: job.id,
        attemptId,
        message: repair.status,
        data: { loops: repair.loops, blockers: repair.blockers, rejected: repair.rejectedActions },
      });

      if (repair.status === 'not_an_application_form') {
        return finish('failed', 'not_an_application', repair.blockers.join('; ') || 'Not an application form', null, page.url());
      }

      if (repair.status === 'needs_next_step' && repair.nextButtonId) {
        await page.locator(`[${BUTTON_ATTR}="${repair.nextButtonId}"]`).first().click({ timeout: 12_000 });
        stepsCompleted += 1;
        await pause(700, 1600);
        continue;
      }

      if (repair.status === 'needs_human' || repair.status === 'needs_fixes') {
        const reason =
          repair.unanswerable.length > 0
            ? `Needs answers the answer bank does not cover: ${repair.unanswerable.map((u) => u.question).join('; ')}`
            : repair.blockers.join('; ') || 'Form could not be completed automatically';

        const escalation = await escalateToHuman(page, {
          reason,
          jobLabel,
          url: page.url(),
          interactive: options.interactive,
        });

        if (!escalation.resolved) {
          const shot = await captureScreenshot(page, `${job.id}-incomplete`);
          if (shot) recordArtifact('incomplete', shot, { jobId: job.id, attemptId });
          return finish(
            'blocked_human',
            repair.unanswerable.length > 0 ? 'unanswered_question' : 'form_incomplete',
            reason,
            shot,
            page.url(),
          );
        }
        // The operator fixed something; re-assess this page rather than assuming.
        continue;
      }

      // repair.status === 'ready_to_submit'
      const snapshot = await extractFormSnapshot(page);
      const decision = decideSubmission({
        verdictStatus: repair.status,
        autofill,
        snapshot,
        mode: options.mode,
      });

      if (!decision.submit) {
        if (options.mode === 'dry-run') {
          const shot = await captureScreenshot(page, `${job.id}-dryrun`);
          if (shot) recordArtifact('dry-run', shot, { jobId: job.id, attemptId });
          return finish('dry_run_complete', null, decision.reason, shot, page.url());
        }

        const shot = await captureScreenshot(page, `${job.id}-gated`);
        return finish(
          decision.needsHuman ? 'blocked_human' : 'failed',
          'form_incomplete',
          decision.reason,
          shot,
          page.url(),
        );
      }

      recordEvent('SUBMITTING', { jobId: job.id, attemptId });
      const submission = await submitApplication(page, {
        mode: options.mode,
        jobLabel,
        artifactPrefix: job.id,
      });

      if (submission.screenshotPath) {
        recordArtifact('submission', submission.screenshotPath, { jobId: job.id, attemptId });
      }

      if (!submission.submitted) {
        return finish('skipped', null, submission.reason, submission.screenshotPath, submission.finalUrl);
      }

      // A click is not proof. An unconfirmed submission is reported as such so the
      // operator knows which ones to check rather than assuming success.
      return finish(
        submission.confirmed ? 'submitted' : 'blocked_human',
        submission.confirmed ? null : 'submit_unconfirmed',
        submission.reason,
        submission.screenshotPath,
        submission.finalUrl,
      );
    }

    const shot = page ? await captureScreenshot(page, `${job.id}-too-many-steps`) : null;
    return finish(
      'failed',
      'too_many_steps',
      `Application still unfinished after ${config.safety.maxFormSteps} pages`,
      shot,
      page?.url() ?? job.url,
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    log.error('Application threw', { error: message });
    const shot = page ? await captureScreenshot(page, `${job.id}-error`) : null;
    return finish('failed', 'error', message, shot, page?.url() ?? job.url);
  } finally {
    await page?.close().catch(() => undefined);
  }
}

/**
 * On an interstitial with no form, look for a control that leads to one.
 *
 * Only apply-like affordances are followed, and never a submit control, so this
 * cannot accidentally submit something on a page the agent has not understood.
 */
async function followApplyAffordance(page: Page): Promise<boolean> {
  const snapshot = await extractFormSnapshot(page);
  const candidate = snapshot.buttons.find(
    (b) => b.kind !== 'submit' && !b.disabled && /^(apply|apply now|start( your)? application|continue to application)/i.test(b.label.trim()),
  );
  if (!candidate) return false;

  logger.info('Following an apply affordance on an interstitial page', { label: candidate.label });
  await page.locator(`[${BUTTON_ATTR}="${candidate.id}"]`).first().click({ timeout: 12_000 }).catch(() => undefined);
  await pause(800, 1800);
  return true;
}

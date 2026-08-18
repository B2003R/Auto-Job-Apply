import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parse as parseYaml } from 'yaml';
import { z } from 'zod';
import { PROJECT_ROOT } from '../config.ts';

const profileSchema = z.object({
  identity: z.object({
    firstName: z.string().min(1),
    lastName: z.string().min(1),
    email: z.string().email(),
    phone: z.string().min(1),
  }),
  location: z.object({
    street: z.string().default(''),
    city: z.string().default(''),
    state: z.string().default(''),
    postalCode: z.string().default(''),
    country: z.string().default(''),
  }),
  links: z
    .object({
      linkedin: z.string().default(''),
      github: z.string().default(''),
      portfolio: z.string().default(''),
    })
    .default({ linkedin: '', github: '', portfolio: '' }),
  currentRole: z
    .object({
      title: z.string().default(''),
      company: z.string().default(''),
      startDate: z.string().default(''),
      endDate: z.string().default(''),
    })
    .optional(),
  education: z
    .array(
      z.object({
        degree: z.string().default(''),
        school: z.string().default(''),
        graduationYear: z.union([z.number(), z.string()]).optional(),
        gpa: z.union([z.number(), z.string()]).optional(),
      }),
    )
    .default([]),
});

const answersSchema = z.object({
  standard: z.record(z.string(), z.string()).default({}),
  eeo: z.record(z.string(), z.string()).default({}),
  skillYears: z.record(z.string(), z.string()).default({}),
  custom: z
    .array(z.object({ match: z.string().min(1), answer: z.string().min(1) }))
    .default([]),
});

export type Profile = z.infer<typeof profileSchema>;
export type Answers = z.infer<typeof answersSchema>;

export interface AnswerBank {
  profile: Profile;
  answers: Answers;
}

let cache: AnswerBank | null = null;

/**
 * Load the operator's personal data. This is the only permitted source of facts
 * about the applicant; the verifier is told it may not invent values, so a
 * missing entry surfaces as an escalation rather than a plausible fabrication.
 */
export function loadAnswerBank(): AnswerBank {
  if (cache) return cache;

  const profilePath = resolve(PROJECT_ROOT, 'config/profile.yaml');
  const answersPath = resolve(PROJECT_ROOT, 'config/answers.yaml');

  for (const [path, example] of [
    [profilePath, 'config/profile.example.yaml'],
    [answersPath, 'config/answers.example.yaml'],
  ] as const) {
    if (!existsSync(path)) {
      throw new Error(`Missing ${path}. Copy ${example} to it and fill in your details.`);
    }
  }

  const profile = profileSchema.safeParse(parseYaml(readFileSync(profilePath, 'utf8')));
  if (!profile.success) {
    throw new Error(`Invalid config/profile.yaml:\n${issues(profile.error)}`);
  }

  const answers = answersSchema.safeParse(parseYaml(readFileSync(answersPath, 'utf8')));
  if (!answers.success) {
    throw new Error(`Invalid config/answers.yaml:\n${issues(answers.error)}`);
  }

  for (const entry of answers.data.custom) {
    try {
      new RegExp(entry.match, 'i');
    } catch (error) {
      throw new Error(
        `config/answers.yaml: custom entry "${entry.match}" is not a valid regular expression: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
  }

  cache = { profile: profile.data, answers: answers.data };
  return cache;
}

export function resetAnswerBankCache(): void {
  cache = null;
}

/**
 * Deterministic lookup for a question, tried before asking the model.
 *
 * Handling the predictable questions in code is cheaper, faster and more
 * repeatable than a model call, and it keeps the model's job narrow: judging the
 * genuinely ambiguous fields.
 */
export function lookupAnswer(question: string, bank: AnswerBank): string | null {
  const q = question.toLowerCase().replace(/\s+/g, ' ').trim();
  if (!q) return null;

  const { answers, profile } = bank;

  // Identity and contact fields, which are the bulk of every form.
  const identityMatchers: Array<[RegExp, string]> = [
    [/^(first|given)\s*name/, profile.identity.firstName],
    [/^(last|family|sur)\s*name/, profile.identity.lastName],
    [/^full\s*name|^name$/, `${profile.identity.firstName} ${profile.identity.lastName}`],
    [/e-?mail/, profile.identity.email],
    [/phone|mobile|telephone/, profile.identity.phone],
    [/linked-?in/, profile.links.linkedin],
    [/github/, profile.links.github],
    [/portfolio|personal (web)?site/, profile.links.portfolio],
    [/^(street|address(\s*line\s*1)?)/, profile.location.street],
    [/^city|^town/, profile.location.city],
    [/^state|^province|^region/, profile.location.state],
    [/zip|postal/, profile.location.postalCode],
    [/^country/, profile.location.country],
  ];
  for (const [pattern, value] of identityMatchers) {
    if (pattern.test(q) && value) return value;
  }

  // Skill-specific years must be checked before the generic experience answer,
  // otherwise "years of experience with Kubernetes" is answered with the total
  // years of professional experience.
  if (/years/.test(q) || /experience/.test(q)) {
    for (const [skill, years] of Object.entries(answers.skillYears)) {
      if (q.includes(skill.toLowerCase())) return years;
    }
  }

  const standardMatchers: Array<[RegExp, string | undefined]> = [
    [/authoriz(ed|ation) to work|legally authorized|work authorization/, answers.standard.authorizedToWorkInUS],
    [/sponsorship.*(now|future)|future.*sponsorship/, answers.standard.requiresSponsorshipNowOrFuture],
    [/sponsorship|visa/, answers.standard.requiresVisaSponsorship],
    [/relocat/, answers.standard.willingToRelocate],
    [/open to remote|remote work|work remotely|remote position/, answers.standard.openToRemote],
    [/non-?compete/, answers.standard.hasNonCompete],
    [/(salary|compensation).*(expect|desired|requirement)|expected (salary|compensation)/, answers.standard.salaryExpectation],
    [/start date|when (can|could) you start|available to start/, answers.standard.earliestStartDate],
    [/years of (professional |relevant |total )?experience/, answers.standard.yearsOfProfessionalExperience],
    [/highest (level of )?(education|degree)/, answers.standard.highestDegree],
    [/how did you hear|referral source|how did you find/, answers.standard.referralSource],
    [/previously (been )?employed|worked (here|for us|at this company)/, answers.standard.previouslyEmployedHere],
  ];
  for (const [pattern, value] of standardMatchers) {
    if (pattern.test(q) && value) return value;
  }

  const eeoMatchers: Array<[RegExp, string | undefined]> = [
    [/hispanic|latino/, answers.eeo.hispanicOrLatino],
    [/gender|sex\b/, answers.eeo.gender],
    [/race|ethnicity/, answers.eeo.race],
    [/veteran|military/, answers.eeo.veteranStatus],
    [/disab/, answers.eeo.disabilityStatus],
  ];
  for (const [pattern, value] of eeoMatchers) {
    if (pattern.test(q) && value) return value;
  }

  for (const entry of answers.custom) {
    if (new RegExp(entry.match, 'i').test(q)) return entry.answer;
  }

  return null;
}

/** Compact reference block handed to the model alongside the form snapshot. */
export function serialiseBankForPrompt(bank: AnswerBank): string {
  const { profile, answers } = bank;
  return JSON.stringify(
    {
      identity: profile.identity,
      location: profile.location,
      links: profile.links,
      currentRole: profile.currentRole,
      education: profile.education,
      standardAnswers: answers.standard,
      eeoAnswers: answers.eeo,
      skillYears: answers.skillYears,
      otherAnswers: answers.custom.map((c) => ({ question: c.match, answer: c.answer })),
    },
    null,
    2,
  );
}

function issues(error: z.ZodError): string {
  return error.issues.map((i) => `  - ${i.path.join('.') || '(root)'}: ${i.message}`).join('\n');
}

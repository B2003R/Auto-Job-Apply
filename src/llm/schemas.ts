import { z } from 'zod';

/**
 * Actions the verifier may request.
 *
 * Deliberately narrow. There is no submit action and no free-form click: the
 * model can populate fields and advance a multi-page form, but the decision to
 * submit stays in code, gated on an explicit verdict plus a completeness check.
 * Every target is a field or button id that came from the snapshot we handed it,
 * so it cannot address arbitrary parts of the page.
 */
export const repairActionSchema = z.discriminatedUnion('action', [
  z.object({
    action: z.literal('set_text'),
    fieldId: z.string(),
    value: z.string(),
    reason: z.string(),
  }),
  z.object({
    action: z.literal('select_option'),
    fieldId: z.string(),
    value: z.string(),
    reason: z.string(),
  }),
  z.object({
    action: z.literal('set_checkbox'),
    fieldId: z.string(),
    checked: z.boolean(),
    reason: z.string(),
  }),
  z.object({
    action: z.literal('choose_radio'),
    fieldId: z.string(),
    value: z.string(),
    reason: z.string(),
  }),
  z.object({
    action: z.literal('upload_resume'),
    fieldId: z.string(),
    reason: z.string(),
  }),
  z.object({
    action: z.literal('click_button'),
    buttonId: z.string(),
    reason: z.string(),
  }),
]);

export type RepairAction = z.infer<typeof repairActionSchema>;

export const verdictStatuses = [
  'ready_to_submit',
  'needs_fixes',
  'needs_next_step',
  'needs_human',
  'not_an_application_form',
] as const;

export const verdictSchema = z.object({
  status: z.enum(verdictStatuses),
  /** Plain-language reasons the form is not ready, for logs and the daily export. */
  blockers: z.array(z.string()),
  /** Required questions with no answer in the bank, recorded for the operator. */
  unanswerable: z.array(
    z.object({
      fieldId: z.string(),
      question: z.string(),
    }),
  ),
  fixes: z.array(repairActionSchema),
  notes: z.string(),
});

export type Verdict = z.infer<typeof verdictSchema>;

/**
 * JSON Schema for OpenAI structured outputs.
 *
 * Written by hand rather than generated from the Zod schema: strict mode demands
 * every property be listed in `required` and every object carry
 * additionalProperties:false, and keeping it explicit avoids a generator's
 * output drifting from those rules. The Zod schema above still validates the
 * response, so the two are checked against each other at run time.
 */
export const verdictJsonSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['status', 'blockers', 'unanswerable', 'fixes', 'notes'],
  properties: {
    status: { type: 'string', enum: [...verdictStatuses] },
    blockers: { type: 'array', items: { type: 'string' } },
    unanswerable: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['fieldId', 'question'],
        properties: {
          fieldId: { type: 'string' },
          question: { type: 'string' },
        },
      },
    },
    fixes: {
      type: 'array',
      items: {
        anyOf: [
          {
            type: 'object',
            additionalProperties: false,
            required: ['action', 'fieldId', 'value', 'reason'],
            properties: {
              action: { type: 'string', enum: ['set_text'] },
              fieldId: { type: 'string' },
              value: { type: 'string' },
              reason: { type: 'string' },
            },
          },
          {
            type: 'object',
            additionalProperties: false,
            required: ['action', 'fieldId', 'value', 'reason'],
            properties: {
              action: { type: 'string', enum: ['select_option'] },
              fieldId: { type: 'string' },
              value: { type: 'string' },
              reason: { type: 'string' },
            },
          },
          {
            type: 'object',
            additionalProperties: false,
            required: ['action', 'fieldId', 'checked', 'reason'],
            properties: {
              action: { type: 'string', enum: ['set_checkbox'] },
              fieldId: { type: 'string' },
              checked: { type: 'boolean' },
              reason: { type: 'string' },
            },
          },
          {
            type: 'object',
            additionalProperties: false,
            required: ['action', 'fieldId', 'value', 'reason'],
            properties: {
              action: { type: 'string', enum: ['choose_radio'] },
              fieldId: { type: 'string' },
              value: { type: 'string' },
              reason: { type: 'string' },
            },
          },
          {
            type: 'object',
            additionalProperties: false,
            required: ['action', 'fieldId', 'reason'],
            properties: {
              action: { type: 'string', enum: ['upload_resume'] },
              fieldId: { type: 'string' },
              reason: { type: 'string' },
            },
          },
          {
            type: 'object',
            additionalProperties: false,
            required: ['action', 'buttonId', 'reason'],
            properties: {
              action: { type: 'string', enum: ['click_button'] },
              buttonId: { type: 'string' },
              reason: { type: 'string' },
            },
          },
        ],
      },
    },
    notes: { type: 'string' },
  },
} as const;

export type PageKind =
  | 'application_form'
  | 'login_required'
  | 'signup_required'
  | 'email_verification_required'
  | 'captcha'
  | 'confirmation'
  | 'expired_or_closed'
  | 'external_redirect'
  | 'other';

export const pageKinds: PageKind[] = [
  'application_form',
  'login_required',
  'signup_required',
  'email_verification_required',
  'captcha',
  'confirmation',
  'expired_or_closed',
  'external_redirect',
  'other',
];

export const classificationSchema = z.object({
  kind: z.enum(pageKinds as [PageKind, ...PageKind[]]),
  confidence: z.number().min(0).max(1),
  evidence: z.string(),
});

export type Classification = z.infer<typeof classificationSchema>;

export const classificationJsonSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['kind', 'confidence', 'evidence'],
  properties: {
    kind: { type: 'string', enum: pageKinds },
    confidence: { type: 'number' },
    evidence: { type: 'string' },
  },
} as const;

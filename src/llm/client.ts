import OpenAI from 'openai';
import { z } from 'zod';
import { loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { sleep } from '../browser/humanize.ts';

let client: OpenAI | null = null;

function getClient(): OpenAI {
  if (client) return client;
  const { env } = loadConfig();
  client = new OpenAI({ apiKey: env.OPENAI_API_KEY, maxRetries: 0 });
  return client;
}

/** Test seam: inject a stub so verifier logic can be tested without network calls. */
export function setOpenAIClient(stub: OpenAI | null): void {
  client = stub;
}

export interface StructuredRequest<T> {
  system: string;
  user: string;
  /** Base64 PNG. Sent alongside the text so the model sees rendered validation. */
  screenshotBase64?: string;
  schemaName: string;
  jsonSchema: Record<string, unknown>;
  validator: z.ZodType<T>;
  /** Label used in logs to identify which call this was. */
  purpose: string;
}

const RETRYABLE_STATUS = new Set([408, 409, 429, 500, 502, 503, 504]);

/**
 * Ask the model for a strictly-shaped JSON answer.
 *
 * Structured outputs guarantee the response parses against the JSON Schema, but
 * the Zod validator still runs: it is the schema the rest of the code relies on,
 * and validating here means a drift between the two surfaces as a clear error
 * rather than an undefined property deep in the executor.
 */
export async function requestStructured<T>(request: StructuredRequest<T>): Promise<T> {
  const { env } = loadConfig();
  const content: OpenAI.Chat.Completions.ChatCompletionContentPart[] = [
    { type: 'text', text: request.user },
  ];

  if (request.screenshotBase64) {
    content.push({
      type: 'image_url',
      image_url: { url: `data:image/png;base64,${request.screenshotBase64}`, detail: 'high' },
    });
  }

  const started = Date.now();
  let lastError: unknown = null;

  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const response = await getClient().chat.completions.create({
        model: env.OPENAI_MODEL,
        // Deterministic by default: the same form should get the same verdict, and
        // creativity is not a virtue when filling compliance fields.
        temperature: 0,
        messages: [
          { role: 'system', content: request.system },
          { role: 'user', content },
        ],
        response_format: {
          type: 'json_schema',
          json_schema: {
            name: request.schemaName,
            strict: true,
            schema: request.jsonSchema,
          },
        },
      });

      const raw = response.choices[0]?.message.content;
      if (!raw) throw new Error('Model returned an empty response');

      const parsed = request.validator.safeParse(JSON.parse(raw));
      if (!parsed.success) {
        throw new Error(`Response did not match the expected shape: ${parsed.error.message}`);
      }

      logger.debug('LLM call finished', {
        purpose: request.purpose,
        attempt,
        ms: Date.now() - started,
        promptTokens: response.usage?.prompt_tokens,
        completionTokens: response.usage?.completion_tokens,
      });

      return parsed.data;
    } catch (error) {
      lastError = error;
      const status = error instanceof OpenAI.APIError ? error.status : undefined;
      const retryable = status === undefined || RETRYABLE_STATUS.has(status);

      if (!retryable || attempt === 3) break;

      const backoffMs = 800 * 2 ** (attempt - 1);
      logger.warn('LLM call failed; retrying', {
        purpose: request.purpose,
        attempt,
        status,
        backoffMs,
        error: error instanceof Error ? error.message : String(error),
      });
      await sleep(backoffMs);
    }
  }

  throw new Error(
    `LLM call "${request.purpose}" failed: ${lastError instanceof Error ? lastError.message : String(lastError)}`,
  );
}

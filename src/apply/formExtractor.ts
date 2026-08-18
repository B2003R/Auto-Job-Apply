import type { Page } from 'playwright';

export type FieldKind =
  | 'text'
  | 'email'
  | 'tel'
  | 'url'
  | 'number'
  | 'date'
  | 'textarea'
  | 'select'
  | 'checkbox'
  | 'radio'
  | 'file'
  | 'unknown';

export interface FormField {
  /** Handle the LLM refers to. Stamped onto the element as data-agent-field. */
  id: string;
  label: string;
  kind: FieldKind;
  value: string;
  required: boolean;
  disabled: boolean;
  options?: string[];
  error?: string;
  placeholder?: string;
}

export type ButtonKind = 'submit' | 'next' | 'back' | 'save' | 'other';

export interface FormButton {
  id: string;
  label: string;
  kind: ButtonKind;
  disabled: boolean;
}

export interface FormSnapshot {
  url: string;
  title: string;
  headings: string[];
  fields: FormField[];
  buttons: FormButton[];
  /** Page-level error/alert text, which is often where the real blocker appears. */
  errorBanners: string[];
}

/** Attribute used to bind LLM-referenced field ids back to real elements. */
export const FIELD_ATTR = 'data-agent-field';
export const BUTTON_ATTR = 'data-agent-button';

/**
 * Read the page into a structured snapshot.
 *
 * Elements are stamped with a stable attribute during extraction so the repair
 * executor can address exactly the field the model named, with no selector
 * guessing. Extraction is idempotent and cheap, so it is re-run before each
 * action batch to recover from framework re-renders that drop the attribute.
 */
export async function extractFormSnapshot(page: Page): Promise<FormSnapshot> {
  return page.evaluate(
    ([fieldAttr, buttonAttr]) => {
      const isVisible = (el: Element): boolean => {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      };

      const text = (el: Element | null | undefined): string =>
        (el?.textContent ?? '').replace(/\s+/g, ' ').trim();

      /**
       * Resolve a human label, in descending order of trustworthiness. Falls back
       * to nearby text because many ATS forms use styled divs rather than labels.
       */
      const labelFor = (el: HTMLElement): string => {
        const aria = el.getAttribute('aria-label');
        if (aria?.trim()) return aria.trim();

        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
          const joined = labelledBy
            .split(/\s+/)
            .map((id) => text(document.getElementById(id)))
            .filter(Boolean)
            .join(' ');
          if (joined) return joined;
        }

        if (el.id) {
          const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (explicit) return text(explicit);
        }

        const wrapping = el.closest('label');
        if (wrapping) return text(wrapping);

        // Walk up looking for a container that holds a label-ish node.
        let node: HTMLElement | null = el.parentElement;
        for (let depth = 0; node && depth < 4; depth += 1, node = node.parentElement) {
          const candidate = node.querySelector('label, legend, [class*="label" i]');
          if (candidate && !candidate.contains(el)) {
            const found = text(candidate);
            if (found) return found;
          }
        }

        const legend = el.closest('fieldset')?.querySelector('legend');
        if (legend) return text(legend);

        return el.getAttribute('placeholder')?.trim() ?? el.getAttribute('name') ?? '';
      };

      const isRequired = (el: HTMLElement, label: string): boolean => {
        if (el.hasAttribute('required')) return true;
        if (el.getAttribute('aria-required') === 'true') return true;
        // A trailing asterisk in the label is the near-universal convention, and
        // is often the only signal on custom widgets that skip the attribute.
        if (/\*\s*$/.test(label) || /\(required\)/i.test(label)) return true;
        const container = el.closest('[class*="required" i]');
        return container !== null;
      };

      const errorFor = (el: HTMLElement): string | undefined => {
        if (el.getAttribute('aria-invalid') === 'true') {
          const describedBy = el.getAttribute('aria-describedby');
          if (describedBy) {
            const joined = describedBy
              .split(/\s+/)
              .map((id) => text(document.getElementById(id)))
              .filter(Boolean)
              .join(' ');
            if (joined) return joined;
          }
        }

        let node: HTMLElement | null = el.parentElement;
        for (let depth = 0; node && depth < 3; depth += 1, node = node.parentElement) {
          const candidate = node.querySelector(
            '[class*="error" i]:not(:empty), [class*="invalid" i]:not(:empty), [role="alert"]:not(:empty)',
          );
          if (candidate && !candidate.contains(el)) {
            const found = text(candidate);
            if (found) return found;
          }
        }
        return undefined;
      };

      const kindOf = (el: HTMLElement): string => {
        if (el instanceof HTMLTextAreaElement) return 'textarea';
        if (el instanceof HTMLSelectElement) return 'select';
        if (el instanceof HTMLInputElement) {
          const t = el.type.toLowerCase();
          const known = ['text', 'email', 'tel', 'url', 'number', 'date', 'checkbox', 'radio', 'file'];
          return known.includes(t) ? t : t === 'password' ? 'text' : 'unknown';
        }
        // Custom widgets: combobox-like roles behave as selects for our purposes.
        const role = el.getAttribute('role');
        if (role === 'combobox' || role === 'listbox') return 'select';
        if (role === 'checkbox') return 'checkbox';
        if (role === 'radio') return 'radio';
        return 'unknown';
      };

      const controls = Array.from(
        document.querySelectorAll<HTMLElement>(
          'input, select, textarea, [role="combobox"], [role="listbox"], [role="checkbox"], [role="radio"]',
        ),
      ).filter((el) => {
        if (!isVisible(el)) return false;
        if (el instanceof HTMLInputElement && ['hidden', 'submit', 'button', 'reset', 'image'].includes(el.type)) {
          return false;
        }
        return true;
      });

      const fields: unknown[] = [];
      const seenRadioGroups = new Set<string>();
      let counter = 0;

      for (const el of controls) {
        const kind = kindOf(el);
        const label = labelFor(el);

        // Collapse a radio group into one logical field with its options, which
        // is how a human reads it and how the model should answer it.
        if (kind === 'radio') {
          const name = el.getAttribute('name') ?? '';
          const groupKey = name || label;
          if (seenRadioGroups.has(groupKey)) continue;
          seenRadioGroups.add(groupKey);

          const members = name
            ? Array.from(document.querySelectorAll<HTMLInputElement>(`input[type="radio"][name="${CSS.escape(name)}"]`))
            : [el as HTMLInputElement];

          const id = `f${(counter += 1)}`;
          el.setAttribute(fieldAttr, id);
          for (const member of members) member.setAttribute(`${fieldAttr}-group`, id);

          const selected = members.find((m) => m.checked);
          fields.push({
            id,
            label: label || name,
            kind: 'radio',
            value: selected ? labelFor(selected) || selected.value : '',
            required: members.some((m) => isRequired(m, label)),
            disabled: members.every((m) => m.disabled),
            options: members.map((m) => labelFor(m) || m.value).filter(Boolean),
            error: errorFor(el),
          });
          continue;
        }

        const id = `f${(counter += 1)}`;
        el.setAttribute(fieldAttr, id);

        let value = '';
        let options: string[] | undefined;

        if (el instanceof HTMLSelectElement) {
          value = el.selectedOptions[0]?.textContent?.trim() ?? '';
          options = Array.from(el.options)
            .map((o) => o.textContent?.trim() ?? '')
            .filter(Boolean);
        } else if (el instanceof HTMLInputElement) {
          value = el.type === 'checkbox' ? String(el.checked) : el.value;
          if (el.type === 'file') value = el.files && el.files.length > 0 ? (el.files[0]?.name ?? '') : '';
        } else if (el instanceof HTMLTextAreaElement) {
          value = el.value;
        } else {
          value = text(el) || el.getAttribute('aria-valuetext') || '';
          if (el.getAttribute('role') === 'checkbox') value = el.getAttribute('aria-checked') ?? 'false';
        }

        fields.push({
          id,
          label,
          kind,
          value,
          required: isRequired(el, label),
          disabled: el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement
            ? el.disabled
            : el.getAttribute('aria-disabled') === 'true',
          options,
          error: errorFor(el),
          placeholder: el.getAttribute('placeholder') ?? undefined,
        });
      }

      const classifyButton = (label: string): string => {
        const l = label.toLowerCase();
        if (/^(submit|submit application|apply|send application|finish|complete)/.test(l)) return 'submit';
        if (/(next|continue|save and continue|proceed|review)/.test(l)) return 'next';
        if (/(back|previous)/.test(l)) return 'back';
        if (/^save/.test(l)) return 'save';
        return 'other';
      };

      const buttons: unknown[] = [];
      let buttonCounter = 0;
      const buttonEls = Array.from(
        document.querySelectorAll<HTMLElement>('button, input[type="submit"], input[type="button"], [role="button"], a[class*="btn" i]'),
      ).filter(isVisible);

      for (const el of buttonEls) {
        const label =
          text(el) ||
          el.getAttribute('aria-label') ||
          (el instanceof HTMLInputElement ? el.value : '') ||
          '';
        if (!label) continue;
        const id = `b${(buttonCounter += 1)}`;
        el.setAttribute(buttonAttr, id);
        buttons.push({
          id,
          label,
          kind: classifyButton(label),
          disabled: el instanceof HTMLButtonElement || el instanceof HTMLInputElement ? el.disabled : el.getAttribute('aria-disabled') === 'true',
        });
      }

      const errorBanners = Array.from(
        document.querySelectorAll('[role="alert"], [class*="error-banner" i], [class*="alert-danger" i], [class*="form-error" i]'),
      )
        .filter(isVisible)
        .map((el) => text(el))
        .filter((t) => t.length > 0 && t.length < 400);

      const headings = Array.from(document.querySelectorAll('h1, h2, legend'))
        .filter(isVisible)
        .map((el) => text(el))
        .filter(Boolean)
        .slice(0, 12);

      return {
        url: location.href,
        title: document.title,
        headings,
        fields,
        buttons,
        errorBanners: Array.from(new Set(errorBanners)),
      };
    },
    [FIELD_ATTR, BUTTON_ATTR] as const,
  ) as Promise<FormSnapshot>;
}

/**
 * Count form controls that currently hold a value. Used as an autofill progress
 * signal that does not depend on the extension's own UI wording.
 */
export async function countFilledFields(page: Page): Promise<number> {
  return page
    .evaluate(() => {
      const controls = Array.from(
        document.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>('input, select, textarea'),
      );
      let filled = 0;
      for (const el of controls) {
        if (el instanceof HTMLInputElement) {
          if (['hidden', 'submit', 'button', 'reset', 'image'].includes(el.type)) continue;
          if (el.type === 'checkbox' || el.type === 'radio') {
            if (el.checked) filled += 1;
            continue;
          }
          if (el.type === 'file') {
            if (el.files && el.files.length > 0) filled += 1;
            continue;
          }
        }
        if (el.value && el.value.trim().length > 0) filled += 1;
      }
      return filled;
    })
    .catch(() => 0);
}

/** Required fields the snapshot shows as still empty. */
export function unfilledRequiredFields(snapshot: FormSnapshot): FormField[] {
  return snapshot.fields.filter(
    (f) => f.required && !f.disabled && (f.value === '' || f.value === 'false'),
  );
}

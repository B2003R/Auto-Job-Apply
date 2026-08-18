import type { BrowserContext, Page } from 'playwright';

export interface DiscoveredJob {
  source: string;
  externalId?: string;
  url: string;
  company?: string;
  title?: string;
  location?: string;
}

export type ApplyClickResult =
  | { kind: 'navigated'; page: Page; note: string }
  | { kind: 'same_page'; page: Page; note: string }
  | { kind: 'unavailable'; note: string };

export interface JobSource {
  key: string;
  label: string;

  /** Collect jobs from the board's listing pages. */
  collect(context: BrowserContext, limit: number): Promise<DiscoveredJob[]>;

  /**
   * Open the posting and press its apply control, following any redirect to the
   * destination ATS. Returns the page the application form should be on.
   */
  clickApply(context: BrowserContext, job: DiscoveredJob): Promise<ApplyClickResult>;
}

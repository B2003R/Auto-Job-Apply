import { loadConfig } from '../config.ts';
import { logger } from '../logger.ts';
import { launchBrowser, resolveChromePath } from '../browser/chrome.ts';
import { probeLogin, SITE_PROBES, type LoginState } from '../browser/loginProbe.ts';
import { detectJobrightExtension } from '../jobright/extension.ts';
import { confirm, heading, waitForEnter } from '../ui/prompt.ts';

const STATE_ICON: Record<LoginState, string> = {
  'logged-in': '  ok  ',
  'logged-out': ' MISS ',
  unknown: '  ??  ',
};

/**
 * One-time provisioning of the agent's dedicated Chrome profile.
 *
 * Everything the agent needs at run time lives in this profile: the Jobright
 * extension, and active sessions for the job boards and the mailbox. Doing it
 * once here is what lets every later run start without any authentication step.
 */
export async function setupBrowser(): Promise<void> {
  const config = loadConfig({ requireEnv: false });

  process.stdout.write(heading('Auto-apply browser setup'));
  process.stdout.write(
    [
      '',
      `Chrome binary   ${resolveChromePath()}`,
      `Profile dir     ${config.paths.profileDir}`,
      `Debug port      ${config.env.CHROME_DEBUG_PORT}`,
      '',
      'This profile is separate from your everyday Chrome profile on purpose:',
      'Chrome 136 and later refuse to expose a debugging port on the default',
      'profile, so the agent cannot drive it.',
      '',
    ].join('\n'),
  );

  const session = await launchBrowser({ keepAlive: true, startUrl: 'about:blank' });

  try {
    process.stdout.write(heading('Step 1 of 3: install the Jobright autofill extension'));
    process.stdout.write(
      [
        '',
        'A Chrome window is open on the agent profile. In that window:',
        '  1. Install the Jobright Autofill extension from the Chrome Web Store.',
        '  2. Open the extension and sign in to your Jobright account.',
        '  3. Pin it to the toolbar so its in-page button appears reliably.',
        '',
      ].join('\n'),
    );

    const storePage = await session.newPage();
    await storePage
      .goto('https://chromewebstore.google.com/search/jobright', { waitUntil: 'domcontentloaded' })
      .catch(() => undefined);

    await waitForEnter('Press Enter once the extension is installed and signed in... ');

    process.stdout.write(heading('Step 2 of 3: sign in to the sites the agent will use'));
    process.stdout.write('\nOpening each site in a tab. Sign in to every one you plan to use.\n\n');

    for (const probe of SITE_PROBES) {
      const page = await session.newPage();
      await page.goto(probe.url, { waitUntil: 'domcontentloaded' }).catch(() => undefined);
      const requirement = probe.required ? 'required' : 'optional';
      process.stdout.write(`  ${probe.label.padEnd(12)} ${requirement.padEnd(9)} ${probe.url}\n`);
      if (probe.note) process.stdout.write(`  ${' '.repeat(22)} ${probe.note}\n`);
    }

    process.stdout.write(
      [
        '',
        `The mailbox must be the account in APPLICATION_EMAIL (${config.env.APPLICATION_EMAIL ?? 'not set'}),`,
        'because ATS verification emails are read from that tab rather than over IMAP.',
        '',
      ].join('\n'),
    );

    await waitForEnter('Press Enter once you are signed in everywhere you need... ');

    process.stdout.write(heading('Step 3 of 3: verification'));
    process.stdout.write('\n');

    const extension = await detectJobrightExtension(session.context);
    if (extension.installed) {
      process.stdout.write(`  ${STATE_ICON['logged-in']}  Jobright extension detected (id ${extension.id})\n`);
    } else {
      process.stdout.write(
        `  ${STATE_ICON['logged-out']}  Jobright extension not detected. ` +
          'It may simply be idle: Chrome unloads extension service workers when inactive. ' +
          'Open a job application page in the window and confirm the Jobright button appears.\n',
      );
    }

    let missingRequired = false;
    for (const probe of SITE_PROBES) {
      const state = await probeLogin(session.context, probe);
      process.stdout.write(`  ${STATE_ICON[state]}  ${probe.label}\n`);
      if (state !== 'logged-in' && probe.required) missingRequired = true;
    }

    process.stdout.write('\n');
    if (missingRequired) {
      process.stdout.write(
        'At least one required site is not signed in. Sign in and re-run `npm run setup:browser` to re-check.\n\n',
      );
    } else {
      process.stdout.write('Profile looks ready. Next: `npm run collect` then `npm run run:dry`.\n\n');
    }

    const keepOpen = await confirm('Leave this Chrome window open?', true);
    if (!keepOpen) {
      process.stdout.write('Close the Chrome window yourself when finished.\n');
    }
    logger.info('Browser setup finished', { extensionInstalled: extension.installed, missingRequired });
  } finally {
    // keepAlive: detach from CDP but leave the operator's window running.
    await session.close();
  }
}

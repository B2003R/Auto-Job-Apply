import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import type { BrowserContext } from 'playwright';
import { loadConfig } from '../config.ts';
import { listExtensionIds } from '../browser/chrome.ts';

export interface ExtensionInfo {
  installed: boolean;
  id?: string;
  name?: string;
  version?: string;
  /** True when Chrome currently has a live service worker for the extension. */
  active?: boolean;
}

const NAME_PATTERN = /jobright/i;

/**
 * Find the Jobright extension in the agent profile.
 *
 * The profile's Extensions directory is the authoritative source: Chrome unloads
 * idle MV3 service workers, so a service-worker scan alone reports a correctly
 * installed extension as missing. The worker scan is still used, as a live worker
 * additionally tells us the extension is awake.
 */
export async function detectJobrightExtension(context?: BrowserContext): Promise<ExtensionInfo> {
  const fromDisk = findExtensionOnDisk(NAME_PATTERN);

  let active = false;
  if (context) {
    const liveIds = await listExtensionIds(context);
    active = fromDisk ? liveIds.includes(fromDisk.id) : false;
  }

  if (!fromDisk) return { installed: false, active };
  return { installed: true, ...fromDisk, active };
}

interface DiskExtension {
  id: string;
  name: string;
  version: string;
}

function findExtensionOnDisk(pattern: RegExp): DiskExtension | null {
  const { profileDir } = loadConfig({ requireEnv: false }).paths;

  // Chrome may use "Default" or a numbered profile inside the user-data dir.
  const profileCandidates = ['Default', 'Profile 1', 'Profile 2', '.'];

  for (const profile of profileCandidates) {
    const extensionsRoot = join(profileDir, profile, 'Extensions');
    if (!existsSync(extensionsRoot)) continue;

    for (const id of safeReadDir(extensionsRoot)) {
      const versionsRoot = join(extensionsRoot, id);
      for (const version of safeReadDir(versionsRoot)) {
        const manifestPath = join(versionsRoot, version, 'manifest.json');
        if (!existsSync(manifestPath)) continue;

        const name = readExtensionName(join(versionsRoot, version), manifestPath);
        if (name && pattern.test(name)) {
          return { id, name, version };
        }
      }
    }
  }
  return null;
}

/** Resolve a manifest name, following __MSG_key__ placeholders into _locales. */
function readExtensionName(versionDir: string, manifestPath: string): string | null {
  try {
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as {
      name?: string;
      default_locale?: string;
    };
    const raw = manifest.name;
    if (!raw) return null;

    const placeholder = /^__MSG_(.+)__$/.exec(raw);
    if (!placeholder?.[1]) return raw;

    const locales = [manifest.default_locale ?? 'en', 'en', 'en_US'];
    for (const locale of locales) {
      const messagesPath = join(versionDir, '_locales', locale, 'messages.json');
      if (!existsSync(messagesPath)) continue;
      const messages = JSON.parse(readFileSync(messagesPath, 'utf8')) as Record<string, { message?: string }>;
      const resolved = messages[placeholder[1]]?.message;
      if (resolved) return resolved;
    }
    return raw;
  } catch {
    return null;
  }
}

function safeReadDir(path: string): string[] {
  try {
    return readdirSync(path, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name);
  } catch {
    return [];
  }
}

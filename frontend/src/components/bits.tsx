'use client';

import { useCallback, useState } from 'react';

import type { AgentProfile } from '@/lib/types';
import type { Connection } from '@/lib/useMatchFeed';

export function CopyButton({
  value,
  label = 'Copy',
  className,
}: {
  value: string;
  label?: string;
  className?: string;
}) {
  const [done, setDone] = useState(false);
  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Clipboard API needs a secure context; fall back to a selection prompt.
      window.prompt('Copy this:', value);
    }
    setDone(true);
    setTimeout(() => setDone(false), 1400);
  }, [value]);

  return (
    <button type="button" onClick={copy} className={className}>
      {done ? 'Copied' : label}
    </button>
  );
}

export function ConnectionChip({ connection }: { connection: Connection }) {
  const text =
    connection === 'open' ? 'live' : connection === 'connecting' ? 'connecting' : 'offline';
  return (
    <span className="chip">
      <span className={`dot ${connection}`} /> {text}
    </span>
  );
}

/** Profile is stored verbatim and never parsed by the platform (spec 6.1) — the UI
 *  surfaces the recommended keys and quietly ignores anything else. */
export function ProfileChips({ profile }: { profile?: AgentProfile }) {
  if (!profile) return null;
  const items: string[] = [];
  if (profile.model) items.push(String(profile.model));
  if (profile.provider) items.push(String(profile.provider));
  if (profile.prompt_version) items.push(`prompt ${profile.prompt_version}`);
  if (profile.temperature !== undefined) items.push(`temp ${profile.temperature}`);
  if (profile.agent_version) items.push(String(profile.agent_version));
  if (items.length === 0) return null;
  return (
    <div className="profile">
      {items.map((item) => (
        <span className="chip" key={item}>
          {item}
        </span>
      ))}
    </div>
  );
}

/**
 * Data access. The backend API (backed by the database) is the single source of
 * truth: run history, a run's venues and decision trails, and the generated
 * images (served as Supabase bucket URLs) all come from here.
 */

import type { ResultsPayload, RunStatus, RunSummary } from '../types';

/**
 * Resolve the backend base URL.
 *
 * 1. An explicit VITE_API_BASE_URL always wins (local override, or a deployed
 *    backend on Vercel/Render).
 * 2. Otherwise, if the page is being served from localhost, assume the backend
 *    is on its default port. This means local development gets the Run button
 *    without depending on a gitignored .env.local file existing — a missing file
 *    should not silently remove a feature.
 * 3. On any other host we return '', so a deployed static build never depends on
 *    a backend that may not be there. That guarantee is unchanged.
 */
function resolveApiBase(): string {
  const configured = (import.meta.env.VITE_API_BASE_URL ?? '').trim().replace(/\/$/, '');
  if (configured) return configured;
  if (typeof window !== 'undefined') {
    const host = window.location.hostname;
    if (host === 'localhost' || host === '127.0.0.1') return 'http://localhost:8001';
  }
  return '';
}

const API_BASE: string = resolveApiBase();

/** Reads tolerate a free-tier cold start (~50s). */
const API_READ_TIMEOUT_MS = 75_000;

/** Starting a run may also have to wake the instance first. */
const API_RUN_TIMEOUT_MS = 90_000;

/** The connection lifecycle, for a small status indicator. */
export type BackendState = 'none' | 'connecting' | 'live' | 'unreachable';

export function hasBackend(): boolean {
  return API_BASE.length > 0;
}

async function fetchWithTimeout(url: string, init: RequestInit = {}, ms: number) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), ms);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timer);
  }
}

// --------------------------------------------------------------------------- //
// Reads — all from the API / database
// --------------------------------------------------------------------------- //

/**
 * One run's full payload. `runKey` selects a historical run; omit for the latest.
 * Throws if the backend cannot be reached, so the caller can show a connecting
 * state and retry.
 */
export async function loadResults(runKey?: string | null): Promise<ResultsPayload> {
  const url = runKey
    ? `${API_BASE}/api/results?run=${encodeURIComponent(runKey)}`
    : `${API_BASE}/api/results`;
  const res = await fetchWithTimeout(url, { cache: 'no-store' }, API_READ_TIMEOUT_MS);
  if (!res.ok) throw new Error(`API returned HTTP ${res.status}`);
  return (await res.json()) as ResultsPayload;
}

/** Run history, newest first. Empty on any failure. */
export async function listRuns(): Promise<RunSummary[]> {
  if (!hasBackend()) return [];
  try {
    const res = await fetchWithTimeout(`${API_BASE}/api/runs`, { cache: 'no-store' }, 20_000);
    if (!res.ok) return [];
    return (await res.json()) as RunSummary[];
  } catch {
    return [];
  }
}

/**
 * Resolve an image reference to a URL.
 *
 * Images are stored in the Supabase bucket and the API returns absolute public
 * URLs, so this is normally a pass-through. The relative fallback only exists so
 * a stray relative path still resolves against the API host rather than breaking.
 */
export function imageUrl(pathOrUrl: string): string {
  if (/^https?:\/\//.test(pathOrUrl)) return pathOrUrl;
  return `${API_BASE}/${pathOrUrl.replace(/^\//, '')}`;
}

// --------------------------------------------------------------------------- //
// Control
// --------------------------------------------------------------------------- //

export interface RunSettingsInput {
  max_venues?: number;
  target_accepted?: number;
}

export interface StartRunResult {
  run_id: string;
  /** Set when the server handed us an already-running run instead of a new one. */
  adopted?: boolean;
}

/**
 * Start a run, or adopt the one already in flight.
 *
 * The backend caps concurrency at one and rate-limits per hour, because the
 * endpoint is unauthenticated by design. Both of those are normal responses to
 * handle, not errors to explode on:
 *   409 → someone is already running one; watch that instead.
 *   429 → hourly cap spent; say so plainly.
 */
export async function startRun(settings?: RunSettingsInput): Promise<StartRunResult> {
  const res = await fetchWithTimeout(
    `${API_BASE}/api/run`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings ?? {}),
    },
    API_RUN_TIMEOUT_MS,
  );

  if (res.status === 409) {
    const body = await res.json().catch(() => null);
    const runId = body?.detail?.run_id;
    if (runId) return { run_id: runId as string, adopted: true };
    throw new Error('A run is already in progress.');
  }

  if (res.status === 429) {
    const body = await res.json().catch(() => null);
    const wait = body?.detail?.retry_after_seconds;
    throw new Error(
      body?.detail?.message ??
        `Rate limited${wait ? `, try again in ${Math.ceil(wait / 60)} min` : ''}.`,
    );
  }

  if (!res.ok) throw new Error(`Failed to start run (HTTP ${res.status})`);

  const data = (await res.json()) as { run_id: string };
  return { run_id: data.run_id };
}

export async function getStatus(runId: string): Promise<RunStatus> {
  const res = await fetchWithTimeout(`${API_BASE}/api/status/${runId}`, {}, 20_000);
  if (!res.ok) throw new Error(`Failed to poll status (HTTP ${res.status})`);
  return (await res.json()) as RunStatus;
}

export interface SettingBound {
  default: number;
  min: number;
  max: number;
}

export interface Health {
  status: string;
  maps_key_configured: boolean;
  gemini_key_configured: boolean;
  dry_run: boolean;
  persistence: 'on' | 'off';
  persistence_detail: string | null;
  bucket: string | null;
  settings: {
    max_venues: SettingBound;
    target_accepted: SettingBound;
  };
}

export async function getHealth(): Promise<Health | null> {
  if (!hasBackend()) return null;
  try {
    const res = await fetchWithTimeout(`${API_BASE}/api/health`, {}, API_READ_TIMEOUT_MS);
    if (!res.ok) return null;
    return (await res.json()) as Health;
  } catch {
    return null;
  }
}

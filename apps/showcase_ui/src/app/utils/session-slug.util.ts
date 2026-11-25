/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

const UUID_REGEX = /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i;
const CHAT_CONTEXT_MARKER = '[Chat context: earlier tasks in this conversation]';

/**
 * Convert a session goal into a clean, URL-safe ASCII slug.
 */
export function slugifyGoal(goal?: string | null, maxLength: number = 48): string {
  if (!goal) return '';
  const markerIdx = goal.indexOf(CHAT_CONTEXT_MARKER);
  const rawGoal = (markerIdx >= 0 ? goal.slice(0, markerIdx) : goal).trim();
  if (!rawGoal) return '';

  const ascii = rawGoal
    .replace(/đ/g, 'd')
    .replace(/Đ/g, 'D')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');

  if (!ascii) return '';
  if (ascii.length <= maxLength) return ascii;

  const sliced = ascii.slice(0, maxLength).replace(/-+$/g, '');
  const lastDash = sliced.lastIndexOf('-');
  return lastDash > 16 ? sliced.slice(0, lastDash) : sliced;
}

/**
 * Build the URL segment for a session using its bare UUID/ID.
 */
export function buildSessionSlug(sessionId: string, _goal?: string | null): string {
  return String(sessionId || '').trim();
}

/**
 * Recover the underlying session UUID/ID from either a `<slug>--<id>` or bare `<id>`.
 */
export function extractSessionIdFromSlug(slugOrId?: string | null): string | null {
  if (!slugOrId) return null;
  let decoded = String(slugOrId).trim();
  try {
    decoded = decodeURIComponent(decoded).trim();
  } catch {
    // Keep original trimmed value on malformed percent-encoding
  }
  if (!decoded) return null;

  const sepIdx = decoded.lastIndexOf('--');
  if (sepIdx >= 0) {
    const tail = decoded.slice(sepIdx + 2).trim();
    if (tail) return tail;
  }

  const uuidMatch = decoded.match(UUID_REGEX);
  if (uuidMatch) return uuidMatch[1];

  return decoded;
}


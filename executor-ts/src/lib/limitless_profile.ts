/**
 * Limitless Exchange profile resolver — Phase audit-5 (15.05.2026).
 *
 * `POST /orders` requires an `ownerId` field (the wallet's profile id,
 * not the wallet address). Without it the server responds with
 * `HTTP 400 {"message":"Bad Request"}` — vague, but reproducible.
 * The id is looked up via `GET /profiles/{address}` (returns the
 * profile object with `id`); we cache per-address in-process so the
 * fast path is a no-op after the first miss.
 *
 * NOT proxied — public profile read, no L2 auth needed, no
 * IP↔wallet correlation to break. Same rule as `sx_fetch.ts`:
 * residential proxy is reserved for the order POST.
 */

const LIMITLESS_PROFILES_URL = 'https://api.limitless.exchange/profiles';
const PROFILE_FETCH_TIMEOUT_MS = 4_000;

interface LimitlessProfile {
  id?: number;
  ownerId?: number;
  [k: string]: unknown;
}

const _cache = new Map<string, number>();

function normalizeAddr(addr: string): string {
  return addr.toLowerCase();
}

/**
 * Resolve the Limitless profile id (= `ownerId` for the POST /orders
 * body) for an EOA address. Throws if the profile doesn't exist —
 * the operator has to register the wallet at limitless.exchange first.
 */
export async function getLimitlessOwnerId(address: string): Promise<number> {
  const key = normalizeAddr(address);
  const cached = _cache.get(key);
  if (cached !== undefined) return cached;

  const url = `${LIMITLESS_PROFILES_URL}/${address}`;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), PROFILE_FETCH_TIMEOUT_MS);
  let resp: Response;
  try {
    resp = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: ac.signal,
    });
  } finally {
    clearTimeout(timer);
  }
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(
      `limitless profile lookup failed: HTTP ${resp.status} for ${address}` +
        (body ? ` body=${body.slice(0, 200)}` : ''),
    );
  }
  const parsed = (await resp.json()) as LimitlessProfile;
  const id = parsed.id ?? parsed.ownerId;
  if (typeof id !== 'number' || !Number.isFinite(id)) {
    throw new Error(
      `limitless profile for ${address} has no numeric id (got ${JSON.stringify(parsed).slice(0, 200)})`,
    );
  }
  _cache.set(key, id);
  return id;
}

/** Test-only — reset the in-process cache so tests can re-stub fetch. */
export function _resetLimitlessProfileCache(): void {
  _cache.clear();
}

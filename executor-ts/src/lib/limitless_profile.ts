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
// Once we've been rate-limited (CF 1015), back off for the full window
// instead of retrying on every fire. 5 minutes ≈ CF 1015 ban length and
// also caps the bandwidth hit on the radar's scan.
const RATE_LIMIT_BACKOFF_MS = 5 * 60 * 1000;

interface LimitlessProfile {
  id?: number;
  ownerId?: number;
  [k: string]: unknown;
}

const _cache = new Map<string, number>();
/** address (lowercased) -> wall-clock ms when we can retry. */
const _rateLimitedUntil = new Map<string, number>();

function normalizeAddr(addr: string): string {
  return addr.toLowerCase();
}

/**
 * Resolve the Limitless profile id (= `ownerId` for the POST /orders
 * body) for an EOA address.
 *
 * Resolution order:
 *   1. `LIMITLESS_OWNER_ID` env (operator hard-coded — cheapest, no
 *      HTTP call). Applies to every wallet uniformly. Set when the
 *      operator already knows the id from limitless.exchange UI.
 *   2. `LIMITLESS_OWNER_ID_<UPPERCASE_ADDR>` env (per-wallet override
 *      for multi-bot deploys).
 *   3. In-process cache from a previous successful lookup.
 *   4. `GET /profiles/{address}` (no auth). Result cached in-process.
 *
 * On CF 1015 rate-limit we cache the negative result for
 * RATE_LIMIT_BACKOFF_MS so repeated fires don't keep hammering CF and
 * keep getting banned — surfaces the same error fast.
 *
 * Throws if no id can be resolved and no env override is present.
 */
export async function getLimitlessOwnerId(address: string): Promise<number> {
  // 1 + 2: env override (global or per-address)
  const envOverride = resolveEnvOverride(address);
  if (envOverride !== null) return envOverride;

  const key = normalizeAddr(address);
  // 3: in-process cache
  const cached = _cache.get(key);
  if (cached !== undefined) return cached;

  // negative-cache: if we got rate-limited recently, fail fast.
  const blockedUntil = _rateLimitedUntil.get(key) ?? 0;
  if (blockedUntil > Date.now()) {
    throw new Error(
      `limitless profile lookup skipped: previous request was rate-limited by CF (Error 1015). ` +
        `Set LIMITLESS_OWNER_ID env to bypass this lookup entirely. ` +
        `Backoff expires in ${Math.ceil((blockedUntil - Date.now()) / 1000)}s.`,
    );
  }

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
    // CF 1015 (rate limit) → trip the negative cache so we stop
    // burning fires + bandwidth on the same lookup. Operator can
    // hard-code LIMITLESS_OWNER_ID once and skip this codepath.
    if (resp.status === 429 || body.includes('Error 1015')) {
      _rateLimitedUntil.set(key, Date.now() + RATE_LIMIT_BACKOFF_MS);
    }
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

function resolveEnvOverride(address: string): number | null {
  const perWalletKey = `LIMITLESS_OWNER_ID_${address.toUpperCase().replace(/^0X/, '')}`;
  const perWallet = process.env[perWalletKey];
  if (perWallet && perWallet.length > 0) {
    const n = Number.parseInt(perWallet, 10);
    if (Number.isFinite(n)) return n;
  }
  const global = process.env['LIMITLESS_OWNER_ID'];
  if (global && global.length > 0) {
    const n = Number.parseInt(global, 10);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

/** Test-only — reset the in-process caches so tests can re-stub fetch. */
export function _resetLimitlessProfileCache(): void {
  _cache.clear();
  _rateLimitedUntil.clear();
}

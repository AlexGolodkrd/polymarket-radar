/**
 * WS manager — singleton registry of user-channel WS clients.
 *
 * Lives outside server.ts to avoid the circular dependency
 *   server.ts → atomic.ts → server.ts
 * that would happen if atomic.ts imported `getPolyUserWS` directly
 * from server.ts (server.ts imports `fireArb` from atomic.ts).
 *
 * server.ts populates this manager at startup; atomic.ts queries it
 * during real-mode firing to pre-subscribe to a market's
 * conditionId BEFORE issuing the POST, so the trade event lands in
 * fillRegistry quickly when the order matches.
 *
 * Phase TS-5c.3 (11.05.2026) — wires the missing piece between the
 * WS listeners (TS-5b1/b2) and the fire path (TS-5c.2).
 */
import type { PolyUserWS } from './poly_user_ws.js';
import type { LimitlessUserWS } from './limitless_user_ws.js';

let _polyUserSockets: PolyUserWS[] = [];
let _limitlessUserSockets: LimitlessUserWS[] = [];

/** Replace the full set (called from server.ts startServer). */
export function setSockets(
  poly: PolyUserWS[],
  lim: LimitlessUserWS[],
): void {
  _polyUserSockets = poly;
  _limitlessUserSockets = lim;
}

/** Lookup PolyUserWS by botId. Used by atomic.ts to pre-subscribe. */
export function getPolyUserWS(botId: string): PolyUserWS | undefined {
  return _polyUserSockets.find((ws) => ws.getMetrics().botId === botId);
}

/** Lookup LimitlessUserWS by botId. Symmetry with getPolyUserWS. */
export function getLimitlessUserWS(botId: string): LimitlessUserWS | undefined {
  return _limitlessUserSockets.find(
    (ws) => ws.getMetrics().botId === botId,
  );
}

/** Read-only snapshot for /metrics. */
export function getAllPolySockets(): PolyUserWS[] {
  return [..._polyUserSockets];
}

export function getAllLimitlessSockets(): LimitlessUserWS[] {
  return [..._limitlessUserSockets];
}

/** Cleanup — called on app close. */
export function stopAll(): void {
  for (const ws of _polyUserSockets) ws.stop();
  for (const ws of _limitlessUserSockets) ws.stop();
}

/** For test reset. */
export function _resetSockets(): void {
  _polyUserSockets = [];
  _limitlessUserSockets = [];
}

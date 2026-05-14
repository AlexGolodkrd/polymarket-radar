/**
 * Limitless Exchange Socket.IO user-channel listener — TS port of
 * `Scripts/limitless_ws.py` (the auth-channel parts).
 *
 *   Endpoint:   wss://ws.limitless.exchange
 *   Namespace:  /markets
 *   Transport:  websocket only (no polling fallback)
 *   Auth:       X-API-Key header at handshake. Without it the connection
 *               still works for public market data, but `orderEvent` and
 *               `positions` channels stay silent.
 *
 * What this module does:
 *   - Connect to /markets, optionally with X-API-Key.
 *   - Re-emit on every `orderEvent` push and bridge each one into
 *     `fillRegistry.consumeByOrderId('limitless', orderId, ...)` so
 *     atomic.fireArb can wake from its dead-man wait.
 *   - Heartbeat: poll every 5s; if no message in 90s (configurable via
 *     LIM_WS_HEARTBEAT_TIMEOUT) → force reconnect (Phase 14a parity:
 *     Socket.IO can stay `connected:true` while silent zombie).
 *
 * What this module does NOT do (deliberate, mirrors Python intent):
 *   - Public orderbook streaming. We treat user-channel and market-data
 *     as separate concerns; market data lives in the Python radar's
 *     existing client. TS executor only needs fills.
 *   - Position reconciliation. Phase TS-5e wires that into TS risk.
 *
 * One instance per wallet that has `limitlessApiKey`. Without the key
 * the client is a no-op (`start()` early-returns).
 *
 * Test seam: Socket.IO client construction is delegated to a `sioFactory`.
 * Tests inject a fake that emits events synchronously without touching
 * the network.
 */
import { EventEmitter } from 'node:events';
import { io as ioClient, type Socket } from 'socket.io-client';
import { registry as fillRegistry } from '../executor/fills.js';
import type { Wallet } from '../types/wallet.js';

const WS_URL = 'wss://ws.limitless.exchange';
const WS_NAMESPACE = '/markets';
const HEARTBEAT_POLL_MS = 5_000;
// 300s (5min) default: user-channel WS only receives messages when the bot
// has open orders/positions producing `orderEvent` / `positions` updates.
// A bot sitting idle (no fires yet) gets zero messages for minutes, and
// the old 90s default would force-reconnect every 90s, churning
// ~40 reconnects/hour for nothing. Env-overridable via
// LIM_USER_WS_HEARTBEAT_TIMEOUT_S.
const DEFAULT_HEARTBEAT_TIMEOUT_S = Number(
  process.env.LIM_USER_WS_HEARTBEAT_TIMEOUT_S || 300,
);
const RECENT_FILLS_CAP = 100;
const RECONNECT_BACKOFF_CAP_MS = 30_000;

/**
 * Minimal subset of `socket.io-client`'s Socket we depend on. The full
 * type is large + version-bumpy; this narrows to the surface we actually
 * use, which makes the test fake easy to build.
 */
export interface SocketLike {
  connected: boolean;
  on(event: string, cb: (...args: unknown[]) => void): this;
  emit(event: string, ...args: unknown[]): unknown;
  disconnect(): unknown;
  connect(): unknown;
}

export type SioFactory = (
  url: string,
  opts: { headers?: Record<string, string>; transports: string[] },
) => SocketLike;

/** Default factory wraps `socket.io-client`'s `io()` into a SocketLike. */
const defaultSioFactory: SioFactory = (url, opts) => {
  const sock = ioClient(`${url}${WS_NAMESPACE}`, {
    transports: opts.transports,
    reconnection: false, // we own the reconnect loop
    extraHeaders: opts.headers,
  });
  return sock as unknown as SocketLike;
};

export interface LimitlessOrderEvent {
  /** Subset — Limitless includes many more fields per docs. */
  orderId?: string;
  marketSlug?: string;
  outcome?: string | number;
  side?: 'BUY' | 'SELL';
  size?: string | number;
  price?: string | number;
  status?: string;
  type?: 'OME' | 'SETTLEMENT';
  [key: string]: unknown;
}

export interface LimitlessUserWSOptions {
  wallet: Wallet;
  onFill?: (ev: LimitlessOrderEvent) => void;
  verbose?: boolean;
  /** Override URL for tests. */
  url?: string;
  /** Override heartbeat timeout in seconds. */
  heartbeatTimeoutS?: number;
  /** Inject a fake io() factory for tests. */
  sioFactory?: SioFactory;
}

export interface LimitlessUserWSMetrics {
  connected: boolean;
  reconnects: number;
  lastMsgAgeSec: number | null;
  msgPerSec: number;
  botId: string;
  authChannelSubscribed: boolean;
  hasApiKey: boolean;
}

export class LimitlessUserWS extends EventEmitter {
  private wallet: Wallet;
  private onFill: (ev: LimitlessOrderEvent) => void;
  private verbose: boolean;
  private url: string;
  private heartbeatTimeoutS: number;
  private sioFactory: SioFactory;

  private sio: SocketLike | null = null;
  private connected = false;
  private orderEventsSubscribed = false;
  private reconnectCount = 0;
  private lastMsgTs = 0;
  private msgWindow: number[] = [];
  private recentFills: LimitlessOrderEvent[] = [];
  private stopFlag = false;
  private supervisorPromise: Promise<void> | null = null;
  private heartbeatTimer: NodeJS.Timeout | null = null;

  constructor(opts: LimitlessUserWSOptions) {
    super();
    this.wallet = opts.wallet;
    this.onFill = opts.onFill ?? (() => {});
    this.verbose = opts.verbose ?? false;
    this.url = opts.url ?? WS_URL;
    this.heartbeatTimeoutS = opts.heartbeatTimeoutS ?? DEFAULT_HEARTBEAT_TIMEOUT_S;
    this.sioFactory = opts.sioFactory ?? defaultSioFactory;
  }

  /** Idempotent. No-op if wallet has no limitlessApiKey. */
  start(): void {
    if (!this.wallet.limitlessApiKey) {
      this.log('no limitlessApiKey — start() is a no-op');
      return;
    }
    if (this.supervisorPromise) return;
    this.stopFlag = false;
    this.supervisorPromise = this.runForever();
  }

  stop(): void {
    this.stopFlag = true;
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.sio) {
      try {
        this.sio.disconnect();
      } catch {
        /* ignore */
      }
      this.sio = null;
    }
  }

  getRecentFills(limit = 20): LimitlessOrderEvent[] {
    return this.recentFills.slice(-limit);
  }

  /**
   * Phase audit (11.05.2026) — TS-next #3: symmetry with Poly's
   * getDesiredMarkets(). Limitless `orderEvent` is a GLOBAL per-API-key
   * channel — there is no per-market filter to maintain — so this returns
   * an empty Set as a sentinel meaning "all markets, no per-market state".
   * Provides API parity for code that iterates both WS managers (e.g.
   * future atomic.ts pre-subscribe logic for Limitless).
   */
  getDesiredMarkets(): Set<string> {
    return new Set();
  }

  getMetrics(): LimitlessUserWSMetrics {
    const now = Date.now() / 1000;
    this.msgWindow = this.msgWindow.filter((t) => now - t < 5);
    return {
      connected: this.connected,
      reconnects: this.reconnectCount,
      lastMsgAgeSec:
        this.lastMsgTs > 0 ? Math.round((now - this.lastMsgTs) * 10) / 10 : null,
      msgPerSec: Math.round((this.msgWindow.length / 5) * 10) / 10,
      botId: this.wallet.botId,
      authChannelSubscribed: this.orderEventsSubscribed,
      hasApiKey: !!this.wallet.limitlessApiKey,
    };
  }

  private async runForever(): Promise<void> {
    while (!this.stopFlag) {
      try {
        await this.connectAndPump();
      } catch (e) {
        this.log(`connect threw: ${(e as Error).message}`);
      }
      this.connected = false;
      this.orderEventsSubscribed = false;
      if (this.stopFlag) break;
      this.reconnectCount++;
      const delayMs = Math.min(
        2 ** Math.min(this.reconnectCount, 5) * 1000,
        RECONNECT_BACKOFF_CAP_MS,
      );
      this.log(`backoff ${delayMs}ms before reconnect`);
      await sleepInterruptible(delayMs, () => this.stopFlag);
    }
  }

  private connectAndPump(): Promise<void> {
    return new Promise((resolve) => {
      // Phase TS-5f.4 (14.05.2026) — WS handshake auth. Limitless V2
      // Trading-scope tokens reject bare X-API-Key — use HMAC headers
      // when a secret is configured. Public market data still works
      // without auth so we tolerate either path.
      let handshakeHeaders: Record<string, string> = {};
      if (this.wallet.limitlessApiKey && this.wallet.limitlessApiSecret) {
        try {
          // Lazy-load to avoid bundling the signer when not used.
          // eslint-disable-next-line @typescript-eslint/no-var-requires
          const { signLmtsRequest } = require('../lib/limitless_hmac.js');
          handshakeHeaders = {
            ...signLmtsRequest(
              this.wallet.limitlessApiKey,
              this.wallet.limitlessApiSecret,
              'GET',
              '/socket.io',
              '',
            ),
          };
        } catch (e) {
          this.log(`HMAC sign failed, falling back to X-API-Key: ${(e as Error).message}`);
          handshakeHeaders = { 'X-API-Key': this.wallet.limitlessApiKey };
        }
      } else if (this.wallet.limitlessApiKey) {
        // Legacy / no-secret path. Will 401 against current API but
        // doesn't crash — public market data path remains usable.
        handshakeHeaders = { 'X-API-Key': this.wallet.limitlessApiKey };
      }

      let sio: SocketLike;
      try {
        sio = this.sioFactory(this.url, {
          headers: handshakeHeaders,
          transports: ['websocket'],
        });
      } catch (e) {
        this.log(`sioFactory threw: ${(e as Error).message}`);
        resolve();
        return;
      }
      this.sio = sio;

      // Wire handlers BEFORE connect so we don't miss the initial frames
      // some Socket.IO servers push immediately on handshake.
      sio.on('connect', () => {
        this.connected = true;
        this.touchMsg();
        this.log('connected to /markets');
        this.subscribeOrderEvents();
        this.startHeartbeat(sio, resolve);
      });

      sio.on('disconnect', () => {
        this.log('disconnected from /markets');
        this.connected = false;
        this.orderEventsSubscribed = false;
        if (this.heartbeatTimer) {
          clearInterval(this.heartbeatTimer);
          this.heartbeatTimer = null;
        }
        if (this.sio === sio) this.sio = null;
        resolve();
      });

      sio.on('orderEvent', (...args: unknown[]) => {
        this.touchMsg();
        const ev = (args[0] ?? {}) as LimitlessOrderEvent;
        this.handleOrderEvent(ev);
      });

      sio.on('newPriceData', () => this.touchMsg());
      sio.on('orderbookUpdate', () => this.touchMsg());
      sio.on('positions', () => this.touchMsg());

      sio.on('authenticated', () => {
        this.touchMsg();
        this.log('authenticated by server (api_key valid)');
      });

      sio.on('exception', (...args: unknown[]) => {
        this.log(`server exception: ${JSON.stringify(args[0] ?? {})}`);
      });

      sio.on('connect_error', (...args: unknown[]) => {
        this.log(`connect_error: ${(args[0] as Error)?.message ?? String(args[0])}`);
      });

      // The default sioFactory auto-connects on construction. If a test
      // factory returns an already-connected mock, the sync emit of
      // 'connect' would fire before our handlers were wired — which is
      // why we attach handlers above the connect() call. For a brand-new
      // socket where auto-connect didn't fire (mocks), we don't need to
      // call sio.connect() because the real ioClient already attempted
      // handshake during construction.
      // Tests that need to drive the connect lifecycle manually do so
      // via fakeSocket.emit('connect').
    });
  }

  private startHeartbeat(sio: SocketLike, resolveOnDeath: () => void): void {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = setInterval(() => {
      if (this.sio !== sio) return;
      const now = Date.now() / 1000;
      const ageS = now - this.lastMsgTs;
      if (this.lastMsgTs > 0 && ageS > this.heartbeatTimeoutS) {
        this.log(`no message in ${ageS.toFixed(0)}s → force reconnect`);
        try {
          sio.disconnect();
        } catch {
          /* ignore */
        }
        if (this.heartbeatTimer) {
          clearInterval(this.heartbeatTimer);
          this.heartbeatTimer = null;
        }
        // Some Socket.IO mocks don't fire `disconnect` after a manual
        // disconnect call; resolve directly so the supervisor loops.
        resolveOnDeath();
      }
    }, HEARTBEAT_POLL_MS);
  }

  private subscribeOrderEvents(): void {
    if (!this.wallet.limitlessApiKey) return;
    if (this.orderEventsSubscribed) return;
    if (!this.sio || !this.connected) return;
    try {
      this.sio.emit('subscribe_order_events', {});
      this.orderEventsSubscribed = true;
      this.log('subscribed to orderEvent (auth channel)');
    } catch (e) {
      this.log(`order_events subscribe failed: ${(e as Error).message}`);
    }
  }

  private handleOrderEvent(ev: LimitlessOrderEvent): void {
    const stamped: LimitlessOrderEvent = {
      ...ev,
      _received_at: Date.now() / 1000,
    };
    this.recentFills.push(stamped);
    if (this.recentFills.length > RECENT_FILLS_CAP) {
      this.recentFills = this.recentFills.slice(-RECENT_FILLS_CAP);
    }
    this.bridgeToFillRegistry(ev);
    try {
      this.onFill(ev);
    } catch (e) {
      this.log(`onFill raised: ${(e as Error).message}`);
    }
  }

  private bridgeToFillRegistry(ev: LimitlessOrderEvent): void {
    const orderId = typeof ev.orderId === 'string' ? ev.orderId : '';
    if (!orderId) return;
    const fillPrice = numericOrZero(ev.price);
    const fillSizeUsdc = fillPrice * numericOrZero(ev.size);
    fillRegistry.consumeByOrderId('limitless', orderId, {
      arbId: '',
      legIdx: 0,
      platform: 'limitless',
      orderId,
      fillPrice,
      fillSizeUsdc,
    });
  }

  private touchMsg(): void {
    this.lastMsgTs = Date.now() / 1000;
    this.msgWindow.push(this.lastMsgTs);
  }

  private log(...args: unknown[]): void {
    if (this.verbose) {
      // eslint-disable-next-line no-console
      console.log(`[LimitlessUserWS-${this.wallet.botId}]`, ...args);
    }
  }
}

function numericOrZero(v: unknown): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string') {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  }
  return 0;
}

async function sleepInterruptible(
  totalMs: number,
  shouldStop: () => boolean,
): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < totalMs) {
    if (shouldStop()) return;
    await new Promise((r) => setTimeout(r, Math.min(100, totalMs)));
  }
}

// Re-export for callers that want the canonical Socket type.
export type { Socket };

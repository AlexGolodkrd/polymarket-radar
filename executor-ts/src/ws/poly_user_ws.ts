/**
 * Polymarket CLOB user-channel WebSocket listener — TS port of
 * `Scripts/poly_user_ws.py`.
 *
 *   Endpoint:  wss://ws-subscriptions-clob.polymarket.com/ws/user
 *   Transport: plain WebSocket (NOT Socket.IO — different from Limitless)
 *   Auth:      first frame after open is the subscribe payload with
 *              {auth:{apiKey,secret,passphrase}, markets:[conditionIds],
 *               type:'user'}
 *
 * Event handling:
 *   - "trade" — full lifecycle MATCHED → MINED → CONFIRMED. We bridge
 *     each into `fillRegistry.consumeByOrderId(...)` so atomic.fireArb
 *     wakes from its dead-man wait inside ~250ms instead of 5s.
 *   - "order" — order placement / update / cancel. Forwarded to caller
 *     via `onOrder` — atomic doesn't latch on these.
 *   - error envelopes ({"error":"unauthorized"}) → enter long backoff
 *     (1h) so we don't hammer Cloudflare with bad creds.
 *
 * One instance per bot wallet — each wallet has its own L2 creds. Without
 * `wallet.polyApiKey` the client is a no-op (`start()` returns immediately,
 * `getMetrics()` shows connected:false). This mirrors the Python side so
 * radar startup never blocks on missing creds.
 *
 * Heartbeat: send "PING" string every 10s, expect "PONG" within 30s
 * (Polymarket's user channel speaks plain text, NOT WS-level ping/pong).
 *
 * Backoff schedule on disconnect: [1, 2, 4, 8, 30] seconds.
 */
import { EventEmitter } from 'node:events';
import WebSocket from 'ws';
import { registry as fillRegistry } from '../executor/fills.js';
import type { Wallet } from '../types/wallet.js';

const WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/user';
const PING_INTERVAL_MS = 10_000;
const PONG_TIMEOUT_MS = 30_000;
const BACKOFF_SCHEDULE_MS = [1_000, 2_000, 4_000, 8_000, 30_000];
const AUTH_FAIL_BACKOFF_MS = 60 * 60 * 1_000; // 1h
const RECENT_FILLS_CAP = 100;

/** Minimal type for the `ws` library WebSocketApp we depend on. */
export interface WSLike {
  send(data: string): void;
  close(code?: number, reason?: string): void;
  on(event: 'open', cb: () => void): this;
  on(event: 'message', cb: (data: Buffer | ArrayBuffer | Buffer[] | string) => void): this;
  on(event: 'error', cb: (err: Error) => void): this;
  on(event: 'close', cb: (code: number, reason: Buffer) => void): this;
}

export type WSFactory = (url: string) => WSLike;

const defaultWSFactory: WSFactory = (url) => new WebSocket(url) as unknown as WSLike;

/** Polymarket trade event (subset — we only read a few fields). */
export interface PolyTradeEvent {
  event_type?: 'trade';
  type?: 'trade';
  status?: string;
  asset_id?: string;
  trade_id?: string;
  market?: string;
  price?: string | number;
  size?: string | number;
  match_size?: string | number;
  side?: 'BUY' | 'SELL';
  /** Some envelopes use `id`, some `order_id`, some `taker_order_id`. */
  id?: string;
  order_id?: string;
  taker_order_id?: string;
  maker_orders?: Array<{ order_id?: string; matched_amount?: string | number }>;
  outcome?: string;
  timestamp?: string | number;
  [key: string]: unknown;
}

export interface PolyOrderEvent {
  event_type?: 'order';
  type?: 'order';
  id?: string;
  market?: string;
  status?: string;
  [key: string]: unknown;
}

export interface PolyUserWSOptions {
  wallet: Wallet;
  onFill?: (ev: PolyTradeEvent) => void;
  onOrder?: (ev: PolyOrderEvent) => void;
  verbose?: boolean;
  /** Allow override for tests (ws://localhost:NNNN). */
  url?: string;
  /** Allow injecting a fake WebSocket factory for tests. */
  wsFactory?: WSFactory;
}

export interface PolyUserWSMetrics {
  subsActive: number;
  subsDesired: number;
  msgPerSec: number;
  reconnects: number;
  lastMsgAgeSec: number | null;
  connected: boolean;
  botId: string;
  authFailedAt: number | null;
}

export class PolyUserWS extends EventEmitter {
  private wallet: Wallet;
  private onFill: (ev: PolyTradeEvent) => void;
  private onOrder: (ev: PolyOrderEvent) => void;
  private verbose: boolean;
  private url: string;
  private wsFactory: WSFactory;

  private desired = new Set<string>();
  private active = new Set<string>();
  private ws: WSLike | null = null;
  private stopFlag = false;
  private heartbeatTimer: NodeJS.Timeout | null = null;
  private lastMsgTs = 0;
  private reconnectCount = 0;
  private connectAttempts = 0;
  private authFailedAt = 0;
  private msgWindow: number[] = [];
  private recentFills: PolyTradeEvent[] = [];
  private supervisorPromise: Promise<void> | null = null;

  constructor(opts: PolyUserWSOptions) {
    super();
    this.wallet = opts.wallet;
    this.onFill = opts.onFill ?? (() => {});
    this.onOrder = opts.onOrder ?? (() => {});
    this.verbose = opts.verbose ?? false;
    this.url = opts.url ?? WS_URL;
    this.wsFactory = opts.wsFactory ?? defaultWSFactory;
  }

  /** Start the supervisor loop. No-op if wallet has no L2 creds. */
  start(): void {
    if (!this.hasCreds()) {
      this.log('no poly creds — start() is a no-op');
      return;
    }
    if (this.supervisorPromise) return; // already running
    this.stopFlag = false;
    this.supervisorPromise = this.runForever();
  }

  /** Stop the supervisor; closes the underlying socket. */
  stop(): void {
    this.stopFlag = true;
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
  }

  /**
   * Replace the desired condition_id set. Forces a reconnect because
   * Polymarket's user channel does not support partial sub/unsub.
   */
  updateMarkets(conditionIds: Iterable<string>): void {
    const next = new Set(Array.from(conditionIds).filter((c) => !!c));
    if (setEquals(next, this.desired)) return;
    this.desired = next;
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
    }
  }

  getMetrics(): PolyUserWSMetrics {
    const now = Date.now() / 1000;
    this.msgWindow = this.msgWindow.filter((t) => now - t < 5);
    return {
      subsActive: this.active.size,
      subsDesired: this.desired.size,
      msgPerSec: Math.round((this.msgWindow.length / 5) * 10) / 10,
      reconnects: this.reconnectCount,
      lastMsgAgeSec:
        this.lastMsgTs > 0 ? Math.round((now - this.lastMsgTs) * 10) / 10 : null,
      connected: this.ws !== null && this.active.size > 0,
      botId: this.wallet.botId,
      authFailedAt: this.authFailedAt > 0 ? this.authFailedAt : null,
    };
  }

  getRecentFills(limit = 20): PolyTradeEvent[] {
    return this.recentFills.slice(-limit);
  }

  /**
   * Phase TS-5c.3 (11.05.2026) — exposed so atomic.ts can MERGE a new
   * conditionId into the existing subscribed set instead of replacing
   * (which would unsub all previous markets and force a reconnect
   * every fire).
   *
   * Returns a defensive copy — caller can mutate without affecting
   * internal state.
   */
  getDesiredMarkets(): Set<string> {
    return new Set(this.desired);
  }

  private hasCreds(): boolean {
    return !!(
      this.wallet.polyApiKey &&
      this.wallet.polySecret &&
      this.wallet.polyPassphrase
    );
  }

  private async runForever(): Promise<void> {
    while (!this.stopFlag) {
      // Auth-failed long backoff — sleep in chunks so stop() responds quickly.
      if (
        this.authFailedAt > 0 &&
        Date.now() - this.authFailedAt < AUTH_FAIL_BACKOFF_MS
      ) {
        await sleepInterruptible(60_000, () => this.stopFlag);
        continue;
      }

      const desired = Array.from(this.desired);
      if (desired.length === 0) {
        await sleepInterruptible(2_000, () => this.stopFlag);
        continue;
      }

      this.connectAttempts++;
      try {
        await this.connectAndPump(desired);
      } catch (e) {
        this.log(`connect exception: ${(e as Error).message}`);
      }
      this.active.clear();
      if (this.stopFlag) break;
      this.reconnectCount++;
      const delayIdx = Math.min(
        this.connectAttempts - 1,
        BACKOFF_SCHEDULE_MS.length - 1,
      );
      const delay = BACKOFF_SCHEDULE_MS[delayIdx] ?? 30_000;
      this.log(`backoff ${delay}ms before reconnect`);
      await sleepInterruptible(delay, () => this.stopFlag);
    }
  }

  /** Open one connection, pump until it closes. Resolves on close. */
  private connectAndPump(markets: string[]): Promise<void> {
    return new Promise((resolve) => {
      let ws: WSLike;
      try {
        ws = this.wsFactory(this.url);
      } catch (e) {
        this.log(`wsFactory threw: ${(e as Error).message}`);
        resolve();
        return;
      }
      this.ws = ws;

      ws.on('open', () => {
        this.active = new Set(markets);
        const payload = {
          auth: {
            apiKey: this.wallet.polyApiKey,
            secret: this.wallet.polySecret,
            passphrase: this.wallet.polyPassphrase,
          },
          markets,
          type: 'user',
        };
        try {
          ws.send(JSON.stringify(payload));
          this.log(`subscribed to ${markets.length} markets (type=user)`);
          this.connectAttempts = 0;
          this.lastMsgTs = Date.now() / 1000;
          this.startHeartbeat(ws);
        } catch (e) {
          this.log(`subscribe send failed: ${(e as Error).message}`);
          try {
            ws.close();
          } catch {
            /* ignore */
          }
        }
      });

      ws.on('message', (data) => {
        const text = typeof data === 'string' ? data : data.toString();
        this.lastMsgTs = Date.now() / 1000;
        this.msgWindow.push(this.lastMsgTs);
        if (text === 'PONG' || text === 'pong') return;
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          return; // malformed JSON; ignore
        }
        const events = Array.isArray(parsed) ? parsed : [parsed];
        for (const ev of events) {
          if (ev && typeof ev === 'object') {
            this.handleEvent(ev as Record<string, unknown>);
          }
        }
      });

      ws.on('error', (err) => {
        this.log(`ws error: ${err.message}`);
      });

      ws.on('close', () => {
        if (this.heartbeatTimer) {
          clearInterval(this.heartbeatTimer);
          this.heartbeatTimer = null;
        }
        if (this.ws === ws) this.ws = null;
        resolve();
      });
    });
  }

  private startHeartbeat(ws: WSLike): void {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = setInterval(() => {
      if (this.ws !== ws) return;
      const ageMs = (Date.now() / 1000 - this.lastMsgTs) * 1000;
      if (this.lastMsgTs > 0 && ageMs > PONG_TIMEOUT_MS) {
        this.log('pong timeout — forcing reconnect');
        try {
          ws.close();
        } catch {
          /* ignore */
        }
        return;
      }
      try {
        ws.send('PING');
      } catch {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      }
    }, PING_INTERVAL_MS);
  }

  private handleEvent(ev: Record<string, unknown>): void {
    // Phase 19v18 parity — auth-error long backoff. Polymarket sends
    // {"error":"unauthorized",...} envelopes when L2 creds are stale or
    // wrong. Without backoff, the supervisor reconnects on 1-30s schedule
    // hammering the server with bad creds → Cloudflare ban risk.
    const errVal = ev.error ?? ev.errorCode;
    if (typeof errVal === 'string') {
      const errLow = errVal.toLowerCase();
      if (
        ['unauthor', 'invalid_api', 'forbidden', '401', '403'].some((k) =>
          errLow.includes(k),
        )
      ) {
        this.log(`auth error from server: ${errVal} — entering long backoff`);
        this.authFailedAt = Date.now();
        // Stop the current connection — runForever loop will sleep in
        // 60s chunks until AUTH_FAIL_BACKOFF_MS passes.
        try {
          this.ws?.close();
        } catch {
          /* ignore */
        }
        return;
      }
    }

    const evType = String(ev.event_type ?? ev.type ?? '').toLowerCase();
    if (evType === 'trade') {
      const trade = ev as PolyTradeEvent;
      const stamped: PolyTradeEvent = { ...trade, _received_at: Date.now() / 1000 };
      this.recentFills.push(stamped);
      if (this.recentFills.length > RECENT_FILLS_CAP) {
        this.recentFills = this.recentFills.slice(-RECENT_FILLS_CAP);
      }
      this.bridgeTradeToRegistry(trade);
      try {
        this.onFill(trade);
      } catch (e) {
        this.log(`onFill raised: ${(e as Error).message}`);
      }
    } else if (evType === 'order') {
      try {
        this.onOrder(ev as PolyOrderEvent);
      } catch (e) {
        this.log(`onOrder raised: ${(e as Error).message}`);
      }
    }
    // Other event types (subscription confirmation, server pings) — ignore
  }

  /**
   * Translate a Poly trade event into one or more `consumeByOrderId`
   * calls on the shared FillRegistry. We don't know which side of the
   * trade corresponds to our pending order, so we try every candidate
   * id field — the registry returns false for the ones that don't match.
   *
   * Trade lifecycle on Polymarket: MATCHED → MINED → CONFIRMED. Atomic
   * registers expecting any of these as a fill (the `consumeByOrderId`
   * is idempotent: only the first match wins, the rest are no-ops).
   */
  private bridgeTradeToRegistry(trade: PolyTradeEvent): void {
    const fillPrice = numericOrZero(trade.price);
    // size is in token units (e.g. 100 YES tokens at $0.55) — convert to USDC
    // by multiplying. This is the convention the Python radar uses too.
    const tokens = numericOrZero(trade.size ?? trade.match_size);
    const fillSizeUsdc = fillPrice * tokens;

    const candidates = collectCandidateOrderIds(trade);
    for (const orderId of candidates) {
      // ev fields arbId/legIdx are dummies — fillRegistry.consumeByOrderId
      // overrides them with the registration's values before emit.
      fillRegistry.consumeByOrderId('polymarket', orderId, {
        arbId: '',
        legIdx: 0,
        platform: 'polymarket',
        orderId,
        fillPrice,
        fillSizeUsdc,
      });
    }
  }

  private log(...args: unknown[]): void {
    if (this.verbose) {
      // eslint-disable-next-line no-console
      console.log(`[PolyUserWS-${this.wallet.botId}]`, ...args);
    }
  }
}

/** Try every plausible field that could carry our order id. */
function collectCandidateOrderIds(trade: PolyTradeEvent): string[] {
  const set = new Set<string>();
  const push = (v: unknown) => {
    if (typeof v === 'string' && v.length > 0) set.add(v);
  };
  push(trade.id);
  push(trade.order_id);
  push(trade.taker_order_id);
  if (Array.isArray(trade.maker_orders)) {
    for (const m of trade.maker_orders) {
      if (m && typeof m === 'object') push(m.order_id);
    }
  }
  return Array.from(set);
}

function numericOrZero(v: unknown): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string') {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  }
  return 0;
}

function setEquals<T>(a: Set<T>, b: Set<T>): boolean {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

/**
 * Sleep that wakes early if `shouldStop()` flips true. Polls every
 * 100ms which is responsive enough for stop() without being noisy.
 */
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

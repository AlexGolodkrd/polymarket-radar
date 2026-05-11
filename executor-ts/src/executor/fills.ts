/**
 * Fill confirmation registry — maps (platform, order_id) → Promise that
 * resolves when the user-channel WS receives the matching fill event.
 *
 * Mirrors Python `Scripts/executor/fills.py:FillRegistry`. Phase TS-3:
 * registry only. WS listeners that consume fills land in TS-5 alongside
 * Polymarket /ws/user, SX Bet /v1/orders/user, Limitless /portfolio
 * connections.
 *
 * The atomic firer awaits these promises with a deadman timeout. If
 * the fill never arrives within the timeout window, the firer cancels
 * the leg and (if other legs filled) reverts them at market.
 */
import { EventEmitter } from 'node:events';

export interface FillEvent {
  arbId: string;
  legIdx: number;
  platform: string;
  orderId?: string;
  slug?: string;
  fillPrice: number;
  fillSizeUsdc: number;
  ts: number;
}

export interface FillRegistration {
  arbId: string;
  legIdx: number;
  platform: string;
  orderId?: string;
  slug?: string;
  registeredAt: number;
}

const REG_TTL_S = 30;

class FillRegistry extends EventEmitter {
  private byOrderId = new Map<string, FillRegistration>();
  private bySlug = new Map<string, FillRegistration>();

  /**
   * Register a pending fill expectation. Returns a promise that
   * resolves with the FillEvent or rejects on timeout.
   */
  register(
    reg: Omit<FillRegistration, 'registeredAt'>,
    timeoutMs = 5000,
  ): Promise<FillEvent> {
    const full: FillRegistration = { ...reg, registeredAt: Date.now() / 1000 };
    if (full.orderId) this.byOrderId.set(this.keyByOrderId(full.platform, full.orderId), full);
    if (full.slug) this.bySlug.set(this.keyBySlug(full.platform, full.slug), full);

    return new Promise((resolve, reject) => {
      const onFill = (ev: FillEvent) => {
        if (ev.arbId === reg.arbId && ev.legIdx === reg.legIdx) {
          clearTimeout(timer);
          this.removeListener('fill', onFill);
          resolve(ev);
        }
      };
      const timer = setTimeout(() => {
        this.removeListener('fill', onFill);
        if (full.orderId) this.byOrderId.delete(this.keyByOrderId(full.platform, full.orderId));
        if (full.slug) this.bySlug.delete(this.keyBySlug(full.platform, full.slug));
        reject(new Error(`fill timeout for arb=${reg.arbId} leg=${reg.legIdx}`));
      }, timeoutMs);
      this.on('fill', onFill);
    });
  }

  /** Called by WS listeners when a fill arrives. Phase TS-5 wires this. */
  consumeByOrderId(platform: string, orderId: string, ev: Omit<FillEvent, 'ts'>): boolean {
    const k = this.keyByOrderId(platform, orderId);
    const reg = this.byOrderId.get(k);
    if (!reg) return false;
    this.byOrderId.delete(k);
    this.emit('fill', { ...ev, arbId: reg.arbId, legIdx: reg.legIdx, ts: Date.now() / 1000 });
    return true;
  }

  consumeBySlug(platform: string, slug: string, ev: Omit<FillEvent, 'ts'>): boolean {
    const k = this.keyBySlug(platform, slug);
    const reg = this.bySlug.get(k);
    if (!reg) return false;
    this.bySlug.delete(k);
    this.emit('fill', { ...ev, arbId: reg.arbId, legIdx: reg.legIdx, ts: Date.now() / 1000 });
    return true;
  }

  /** Periodic janitor — call from a 10s interval to drop stale regs. */
  expireStale(): number {
    const now = Date.now() / 1000;
    let purged = 0;
    for (const [k, r] of this.byOrderId) {
      if (now - r.registeredAt > REG_TTL_S) {
        this.byOrderId.delete(k);
        purged++;
      }
    }
    for (const [k, r] of this.bySlug) {
      if (now - r.registeredAt > REG_TTL_S) {
        this.bySlug.delete(k);
        purged++;
      }
    }
    return purged;
  }

  pendingCount(): number {
    return this.byOrderId.size + this.bySlug.size;
  }

  metrics(): { pending: number; byOrderId: number; bySlug: number } {
    return {
      pending: this.pendingCount(),
      byOrderId: this.byOrderId.size,
      bySlug: this.bySlug.size,
    };
  }

  private keyByOrderId(platform: string, orderId: string): string {
    return `${platform}::${orderId}`;
  }
  private keyBySlug(platform: string, slug: string): string {
    return `${platform}::${slug}`;
  }
}

export const registry = new FillRegistry();

/**
 * Phase TS-5c.3 (11.05.2026) — high-level helper combining `register`,
 * the dead-man wait, slippage evaluation, and a structured outcome
 * report. atomic.fireLeg in real-mode (TS-5c.2) calls this AFTER the
 * real POST returns with an orderId.
 *
 * Returns one of:
 *   - {kind:'filled', fillPrice, fillSizeUsdc, slippage}      — within tolerance
 *   - {kind:'slipped', fillPrice, fillSizeUsdc, slippage}     — beyond tolerance
 *   - {kind:'timeout', reason}                                — no fill in deadmanMs
 *
 * Pure-ish: depends on `registry` singleton + the slippage helper, but
 * has no external I/O of its own. Tests inject events into the registry
 * to drive each kind.
 */
import { evaluateSlippage, type SlippageDecision } from './slippage.js';

export type ExpectFillOutcome =
  | {
      kind: 'filled';
      fillPrice: number;
      fillSizeUsdc: number;
      slippage: SlippageDecision;
    }
  | {
      kind: 'slipped';
      fillPrice: number;
      fillSizeUsdc: number;
      slippage: SlippageDecision;
    }
  | { kind: 'timeout'; reason: string };

export interface ExpectFillInput {
  arbId: string;
  legIdx: number;
  platform: string;
  orderId: string;
  expectedPrice: number;
  /** Deadman timeout in ms — defaults 5000 to mirror Python. */
  deadmanMs?: number;
  /** Optional explicit tolerance override (else uses DEFAULT_SLIPPAGE_TOLERANCE). */
  slippageTolerance?: number;
}

export async function expectFill(
  input: ExpectFillInput,
): Promise<ExpectFillOutcome> {
  const deadmanMs = input.deadmanMs ?? 5000;
  try {
    const ev = await registry.register(
      {
        arbId: input.arbId,
        legIdx: input.legIdx,
        platform: input.platform,
        orderId: input.orderId,
      },
      deadmanMs,
    );
    const slippage = evaluateSlippage(
      input.expectedPrice,
      ev.fillPrice,
      input.slippageTolerance,
    );
    return {
      kind: slippage.within ? 'filled' : 'slipped',
      fillPrice: ev.fillPrice,
      fillSizeUsdc: ev.fillSizeUsdc,
      slippage,
    };
  } catch (err) {
    return {
      kind: 'timeout',
      reason: err instanceof Error ? err.message : String(err),
    };
  }
}

/**
 * Tests for src/ws/poly_user_ws.ts.
 *
 * Strategy: inject a fake `wsFactory` so we never touch the real
 * Polymarket socket. The fake plays the role of `ws.WebSocket` —
 * accepts `.on(event, cb)`, exposes `.send` and `.close`, and lets
 * the test push events synchronously via helper methods. This keeps
 * the suite hermetic + fast (no network, no real timers needed for
 * the connect/subscribe flow).
 *
 * Areas covered:
 *   - no-creds wallet → start() is a no-op
 *   - on-open subscribe payload shape (apiKey/secret/passphrase + markets)
 *   - "trade" event bridges to fillRegistry.consumeByOrderId for every
 *     candidate id field (id / order_id / taker_order_id / maker_orders[])
 *   - PONG / pong text messages are NOT parsed as JSON
 *   - "order" event → onOrder callback fires
 *   - auth-error envelope ({"error":"unauthorized"}) → authFailedAt set
 *   - updateMarkets() with same set is a no-op
 *   - getRecentFills() respects cap + return order
 */
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PolyUserWS, type WSLike, type WSFactory } from '../../src/ws/poly_user_ws.js';
import { registry as fillRegistry } from '../../src/executor/fills.js';
import type { Wallet } from '../../src/types/wallet.js';

// ── Fake WebSocket — drives the listener under test ────────────────
class FakeWS extends EventEmitter implements WSLike {
  url: string;
  sent: string[] = [];
  closeArgs: { code?: number; reason?: string } | null = null;
  constructor(url: string) {
    super();
    this.url = url;
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(code?: number, reason?: string): void {
    this.closeArgs = { ...(code !== undefined ? { code } : {}), ...(reason !== undefined ? { reason } : {}) };
    setImmediate(() => this.emit('close', code ?? 1000, Buffer.from(reason ?? '')));
  }
  // Test helpers
  fireOpen(): void {
    this.emit('open');
  }
  fireMessage(data: string | Buffer): void {
    this.emit('message', data);
  }
  fireError(err: Error): void {
    this.emit('error', err);
  }
}

const baseWallet = (overrides: Partial<Wallet> = {}): Wallet => ({
  botId: 'bot1',
  ethAddress: '0x0000000000000000000000000000000000000001',
  canSign: false,
  signatureType: 0,
  polyApiKey: 'k',
  polySecret: 'cw==',
  polyPassphrase: 'p',
  ...overrides,
});

function makeFactory(): { factory: WSFactory; sockets: FakeWS[] } {
  const sockets: FakeWS[] = [];
  const factory: WSFactory = (url) => {
    const fake = new FakeWS(url);
    sockets.push(fake);
    return fake;
  };
  return { factory, sockets };
}

/** Wait until predicate is true, polling 5ms. Throws after timeout. */
async function waitFor(pred: () => boolean, timeoutMs = 1000, label = 'condition'): Promise<void> {
  const start = Date.now();
  while (!pred()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error(`waitFor timed out: ${label}`);
    }
    await new Promise((r) => setTimeout(r, 5));
  }
}

describe('PolyUserWS', () => {
  // FillRegistry class is not exported (only the singleton); narrow to a
  // method-only shape so vi.spyOn's overload resolution typechecks.
  type RegistryShape = {
    consumeByOrderId: (
      platform: string,
      orderId: string,
      ev: Parameters<typeof fillRegistry.consumeByOrderId>[2],
    ) => boolean;
  };
  const reg = fillRegistry as unknown as RegistryShape;
  let consumeSpy: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    consumeSpy = vi.spyOn(reg, 'consumeByOrderId') as unknown as ReturnType<typeof vi.fn>;
    (consumeSpy as unknown as { mockReturnValue: (v: boolean) => void }).mockReturnValue(false);
  });
  afterEach(() => {
    (consumeSpy as unknown as { mockRestore: () => void }).mockRestore();
  });

  it('start() is a no-op without poly creds', () => {
    const { factory, sockets } = makeFactory();
    const ws = new PolyUserWS({
      wallet: baseWallet({
        polyApiKey: undefined,
        polySecret: undefined,
        polyPassphrase: undefined,
      }),
      url: 'ws://test',
      wsFactory: factory,
    });
    ws.updateMarkets(['c1']);
    ws.start();
    expect(sockets.length).toBe(0);
    expect(ws.getMetrics().connected).toBe(false);
    ws.stop();
  });

  it('sends correct subscribe payload on open', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new PolyUserWS({
      wallet: baseWallet({
        polyApiKey: 'API',
        polySecret: 'SECR',
        polyPassphrase: 'PASS',
      }),
      url: 'ws://test',
      wsFactory: factory,
    });
    ws.updateMarkets(['cond-A', 'cond-B']);
    ws.start();
    await waitFor(() => sockets.length === 1, 500, 'socket created');
    sockets[0]!.fireOpen();
    await waitFor(() => sockets[0]!.sent.length > 0, 500, 'payload sent');
    const body = JSON.parse(sockets[0]!.sent[0]!);
    expect(body.type).toBe('user');
    expect(body.markets).toEqual(['cond-A', 'cond-B']);
    expect(body.auth).toEqual({ apiKey: 'API', secret: 'SECR', passphrase: 'PASS' });
    ws.stop();
  });

  it('PONG text frames are NOT parsed as JSON', async () => {
    const { factory, sockets } = makeFactory();
    const onFill = vi.fn();
    const ws = new PolyUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      wsFactory: factory,
      onFill,
    });
    ws.updateMarkets(['c1']);
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.fireOpen();
    sockets[0]!.fireMessage('PONG');
    sockets[0]!.fireMessage('pong');
    sockets[0]!.fireMessage('not-json');
    expect(onFill).not.toHaveBeenCalled();
    expect(consumeSpy).not.toHaveBeenCalled();
    ws.stop();
  });

  it('trade event bridges every candidate id to fillRegistry', async () => {
    const { factory, sockets } = makeFactory();
    const onFill = vi.fn();
    const ws = new PolyUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      wsFactory: factory,
      onFill,
    });
    ws.updateMarkets(['c1']);
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.fireOpen();
    const trade = {
      event_type: 'trade',
      status: 'MATCHED',
      id: 'top-id',
      order_id: 'order-id',
      taker_order_id: 'taker-id',
      maker_orders: [{ order_id: 'maker-1' }, { order_id: 'maker-2' }],
      price: '0.55',
      size: '100',
    };
    sockets[0]!.fireMessage(JSON.stringify(trade));
    expect(onFill).toHaveBeenCalledTimes(1);
    // Every distinct id should land as a consumeByOrderId call.
    const calledIds = consumeSpy.mock.calls.map((c) => c[1] as string);
    expect(new Set(calledIds)).toEqual(
      new Set(['top-id', 'order-id', 'taker-id', 'maker-1', 'maker-2']),
    );
    // First call's payload should carry numeric price + sizeUsdc = price*tokens.
    const firstPayload = consumeSpy.mock.calls[0]![2] as { fillPrice: number; fillSizeUsdc: number };
    expect(firstPayload.fillPrice).toBeCloseTo(0.55);
    expect(firstPayload.fillSizeUsdc).toBeCloseTo(0.55 * 100);
    ws.stop();
  });

  it('order event triggers onOrder callback', async () => {
    const { factory, sockets } = makeFactory();
    const onOrder = vi.fn();
    const ws = new PolyUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      wsFactory: factory,
      onOrder,
    });
    ws.updateMarkets(['c1']);
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.fireOpen();
    sockets[0]!.fireMessage(
      JSON.stringify({ event_type: 'order', id: 'ord-1', status: 'CANCELED' }),
    );
    expect(onOrder).toHaveBeenCalledTimes(1);
    expect((onOrder.mock.calls[0]![0] as { id?: string }).id).toBe('ord-1');
    expect(consumeSpy).not.toHaveBeenCalled();
    ws.stop();
  });

  it('auth-error envelope flips authFailedAt and closes socket', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new PolyUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      wsFactory: factory,
    });
    ws.updateMarkets(['c1']);
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.fireOpen();
    expect(ws.getMetrics().authFailedAt).toBeNull();
    sockets[0]!.fireMessage(JSON.stringify({ error: 'unauthorized' }));
    expect(ws.getMetrics().authFailedAt).not.toBeNull();
    expect(sockets[0]!.closeArgs).not.toBeNull();
    ws.stop();
  });

  it('updateMarkets with identical set is a no-op (no reconnect)', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new PolyUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      wsFactory: factory,
    });
    ws.updateMarkets(['x', 'y']);
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.fireOpen();
    const beforeReconnects = ws.getMetrics().reconnects;
    ws.updateMarkets(['x', 'y']); // same set
    // Same socket should still be open (no .close called).
    expect(sockets[0]!.closeArgs).toBeNull();
    expect(ws.getMetrics().reconnects).toBe(beforeReconnects);
    ws.stop();
  });

  it('getRecentFills respects cap + returns last N', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new PolyUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      wsFactory: factory,
    });
    ws.updateMarkets(['c1']);
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.fireOpen();
    for (let i = 0; i < 5; i++) {
      sockets[0]!.fireMessage(
        JSON.stringify({ event_type: 'trade', id: `t${i}`, price: '0.5', size: '1' }),
      );
    }
    const last3 = ws.getRecentFills(3);
    expect(last3.length).toBe(3);
    expect((last3[0] as { id?: string }).id).toBe('t2');
    expect((last3[2] as { id?: string }).id).toBe('t4');
    ws.stop();
  });

  it('metrics reflect msg activity + connected state', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new PolyUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      wsFactory: factory,
    });
    ws.updateMarkets(['m1']);
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.fireOpen();
    sockets[0]!.fireMessage(JSON.stringify({ event_type: 'order', id: 'a' }));
    const m = ws.getMetrics();
    expect(m.subsActive).toBe(1);
    expect(m.subsDesired).toBe(1);
    expect(m.connected).toBe(true);
    expect(m.botId).toBe('bot1');
    expect(m.lastMsgAgeSec).not.toBeNull();
    ws.stop();
  });

  it('stop() clears socket and prevents future activity', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new PolyUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      wsFactory: factory,
    });
    ws.updateMarkets(['c1']);
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.fireOpen();
    ws.stop();
    expect(sockets[0]!.closeArgs).not.toBeNull();
    // Even after stop, an inbound message must not crash:
    expect(() =>
      sockets[0]!.fireMessage(JSON.stringify({ event_type: 'trade', id: 'x' })),
    ).not.toThrow();
  });
});

/**
 * Tests for src/ws/limitless_user_ws.ts.
 *
 * Strategy mirrors poly_user_ws tests: inject a `sioFactory` that
 * returns a fake Socket.IO client. The fake is a plain EventEmitter
 * with `emit`/`disconnect` shims, so tests can drive the lifecycle
 * synchronously without a real network or Socket.IO server.
 *
 * Coverage:
 *   - start() no-op without limitlessApiKey
 *   - on connect → subscribe_order_events emitted with empty payload
 *   - orderEvent → fillRegistry.consumeByOrderId('limitless', orderId, ...)
 *   - orderEvent without orderId → no consume call (defensive)
 *   - recentFills cap + ordering
 *   - getMetrics shape (connected, hasApiKey, authChannelSubscribed)
 *   - stop() disconnects + clears state
 *   - X-API-Key header passed to sioFactory when present
 */
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import {
  LimitlessUserWS,
  type SocketLike,
  type SioFactory,
} from '../../src/ws/limitless_user_ws.js';
import { registry as fillRegistry } from '../../src/executor/fills.js';
import type { Wallet } from '../../src/types/wallet.js';

class FakeSocket extends EventEmitter implements SocketLike {
  connected = false;
  emitted: Array<{ event: string; payload: unknown }> = [];
  disconnected = false;
  override emit(event: string, ...args: unknown[]): boolean {
    // Two responsibilities: (1) record outbound emits made by the WS
    // (subscribe_order_events etc.), (2) feed inbound events when the
    // test calls fakeSocket.emit('orderEvent', payload). We branch on
    // a small known list of "outbound" event names and let everything
    // else propagate as inbound via super.emit.
    const OUTBOUND = new Set(['subscribe_order_events', 'subscribe_positions']);
    if (OUTBOUND.has(event)) {
      this.emitted.push({ event, payload: args[0] });
      return true;
    }
    return super.emit(event, ...args);
  }
  disconnect(): void {
    this.disconnected = true;
    this.connected = false;
    super.emit('disconnect');
  }
  connect(): void {
    /* no-op for fake */
  }
}

const baseWallet = (overrides: Partial<Wallet> = {}): Wallet => ({
  botId: 'bot1',
  ethAddress: '0x0000000000000000000000000000000000000001',
  canSign: false,
  signatureType: 0,
  limitlessApiKey: 'test-api-key',
  ...overrides,
});

function makeFactory(): {
  factory: SioFactory;
  sockets: FakeSocket[];
  lastHeaders: Record<string, string> | undefined;
} {
  const sockets: FakeSocket[] = [];
  const ctx: { lastHeaders: Record<string, string> | undefined } = {
    lastHeaders: undefined,
  };
  const factory: SioFactory = (_url, opts) => {
    ctx.lastHeaders = opts.headers;
    const fake = new FakeSocket();
    sockets.push(fake);
    return fake;
  };
  return {
    factory,
    sockets,
    get lastHeaders() {
      return ctx.lastHeaders;
    },
  } as unknown as {
    factory: SioFactory;
    sockets: FakeSocket[];
    lastHeaders: Record<string, string> | undefined;
  };
}

async function waitFor(pred: () => boolean, timeoutMs = 1000, label = '?'): Promise<void> {
  const start = Date.now();
  while (!pred()) {
    if (Date.now() - start > timeoutMs) throw new Error(`waitFor timed out: ${label}`);
    await new Promise((r) => setTimeout(r, 5));
  }
}

describe('LimitlessUserWS', () => {
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

  it('start() is a no-op without limitlessApiKey', () => {
    const { factory, sockets } = makeFactory();
    const ws = new LimitlessUserWS({
      wallet: baseWallet({ limitlessApiKey: undefined }),
      url: 'ws://test',
      sioFactory: factory,
    });
    ws.start();
    expect(sockets.length).toBe(0);
    expect(ws.getMetrics().connected).toBe(false);
    expect(ws.getMetrics().hasApiKey).toBe(false);
    ws.stop();
  });

  it('passes X-API-Key header to sioFactory', async () => {
    const ctx = makeFactory();
    const ws = new LimitlessUserWS({
      wallet: baseWallet({ limitlessApiKey: 'KEY-XYZ' }),
      url: 'ws://test',
      sioFactory: ctx.factory,
    });
    ws.start();
    await waitFor(() => ctx.sockets.length === 1, 500);
    expect(ctx.lastHeaders).toEqual({ 'X-API-Key': 'KEY-XYZ' });
    ws.stop();
  });

  it('on connect emits subscribe_order_events with empty payload', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new LimitlessUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      sioFactory: factory,
    });
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.connected = true;
    sockets[0]!.emit('connect');
    expect(sockets[0]!.emitted).toContainEqual({
      event: 'subscribe_order_events',
      payload: {},
    });
    expect(ws.getMetrics().authChannelSubscribed).toBe(true);
    ws.stop();
  });

  it('orderEvent bridges orderId to fillRegistry', async () => {
    const { factory, sockets } = makeFactory();
    const onFill = vi.fn();
    const ws = new LimitlessUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      sioFactory: factory,
      onFill,
    });
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.connected = true;
    sockets[0]!.emit('connect');
    sockets[0]!.emit('orderEvent', {
      orderId: 'lim-ord-1',
      price: '0.42',
      size: '10',
      status: 'OME',
    });
    expect(onFill).toHaveBeenCalledTimes(1);
    expect(consumeSpy).toHaveBeenCalledTimes(1);
    expect(consumeSpy.mock.calls[0]![0]).toBe('limitless');
    expect(consumeSpy.mock.calls[0]![1]).toBe('lim-ord-1');
    const payload = consumeSpy.mock.calls[0]![2] as {
      fillPrice: number;
      fillSizeUsdc: number;
    };
    expect(payload.fillPrice).toBeCloseTo(0.42);
    expect(payload.fillSizeUsdc).toBeCloseTo(0.42 * 10);
    ws.stop();
  });

  it('orderEvent without orderId — no consume call (defensive)', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new LimitlessUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      sioFactory: factory,
    });
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.connected = true;
    sockets[0]!.emit('connect');
    sockets[0]!.emit('orderEvent', { status: 'SETTLEMENT', size: '5', price: '0.6' });
    expect(consumeSpy).not.toHaveBeenCalled();
    ws.stop();
  });

  it('recentFills cap + ordering', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new LimitlessUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      sioFactory: factory,
    });
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.connected = true;
    sockets[0]!.emit('connect');
    for (let i = 0; i < 7; i++) {
      sockets[0]!.emit('orderEvent', { orderId: `o${i}`, price: '0.5', size: '1' });
    }
    const last3 = ws.getRecentFills(3);
    expect(last3.length).toBe(3);
    expect((last3[0] as { orderId?: string }).orderId).toBe('o4');
    expect((last3[2] as { orderId?: string }).orderId).toBe('o6');
    ws.stop();
  });

  it('getMetrics reports connected + auth state correctly', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new LimitlessUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      sioFactory: factory,
    });
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    expect(ws.getMetrics().connected).toBe(false); // not connected yet
    sockets[0]!.connected = true;
    sockets[0]!.emit('connect');
    sockets[0]!.emit('orderbookUpdate', {});
    const m = ws.getMetrics();
    expect(m.connected).toBe(true);
    expect(m.hasApiKey).toBe(true);
    expect(m.authChannelSubscribed).toBe(true);
    expect(m.botId).toBe('bot1');
    expect(m.lastMsgAgeSec).not.toBeNull();
    ws.stop();
  });

  it('stop() disconnects underlying socket', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new LimitlessUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      sioFactory: factory,
    });
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.connected = true;
    sockets[0]!.emit('connect');
    ws.stop();
    expect(sockets[0]!.disconnected).toBe(true);
    expect(ws.getMetrics().connected).toBe(false);
  });

  it('exception event does not crash listener', async () => {
    const { factory, sockets } = makeFactory();
    const ws = new LimitlessUserWS({
      wallet: baseWallet(),
      url: 'ws://test',
      sioFactory: factory,
    });
    ws.start();
    await waitFor(() => sockets.length === 1, 500);
    sockets[0]!.connected = true;
    sockets[0]!.emit('connect');
    expect(() =>
      sockets[0]!.emit('exception', { code: 500, message: 'server boom' }),
    ).not.toThrow();
    ws.stop();
  });
});

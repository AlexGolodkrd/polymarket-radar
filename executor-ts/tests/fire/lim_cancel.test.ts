/**
 * Tests for deleteLimOrder in src/fire/lim_post.ts.
 *
 * Limitless cancel uses X-API-Key auth only (no HMAC, no signature, no
 * body). DELETE /orders/{id}. Server may return 200+JSON or 204+empty.
 *
 * Strategy: stub globalThis.fetch like the http_client tests, verify
 * URL composition, method, header presence, retry behavior.
 */
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { deleteLimOrder } from '../../src/fire/lim_post.js';
import { HttpError } from '../../src/lib/http_client.js';

describe('deleteLimOrder', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch') as ReturnType<typeof vi.spyOn>;
  });
  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it('sends DELETE to /orders/{orderId} with X-API-Key header', async () => {
    let captured: { url?: string; method?: string; headers?: Record<string, string> } = {};
    (fetchSpy.mockImplementation as (impl: unknown) => void)(
      async (url: unknown, init: unknown) => {
        const i = init as { method?: string; headers?: Record<string, string> } | undefined;
        captured = {
          url: String(url),
          method: i?.method,
          headers: i?.headers,
        };
        return new Response('{"cancelled":true}', { status: 200 });
      },
    );
    await deleteLimOrder({ orderId: 'lim-abc-123', apiKey: 'KEY-XYZ' });
    expect(captured.url).toBe('https://api.limitless.exchange/orders/lim-abc-123');
    expect(captured.method).toBe('DELETE');
    expect(captured.headers?.['X-API-Key']).toBe('KEY-XYZ');
  });

  it('refuses without orderId', async () => {
    await expect(
      deleteLimOrder({ orderId: '', apiKey: 'k' }),
    ).rejects.toThrow(/empty orderId/);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('refuses without apiKey', async () => {
    await expect(
      deleteLimOrder({ orderId: 'ord-1', apiKey: '' }),
    ).rejects.toThrow(/X-API-Key/);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('parses 200 + JSON body into LimCancelResult', async () => {
    fetchSpy.mockResolvedValue(
      new Response('{"cancelled":true,"orderId":"ord-1"}', { status: 200 }),
    );
    const r = await deleteLimOrder({ orderId: 'ord-1', apiKey: 'k' });
    expect(r.status).toBe(200);
    expect(r.body.cancelled).toBe(true);
    expect(r.body.orderId).toBe('ord-1');
  });

  it('synthesizes cancelled:true on 204 No Content', async () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 204 }));
    const r = await deleteLimOrder({ orderId: 'ord-empty', apiKey: 'k' });
    expect(r.status).toBe(204);
    expect(r.body.cancelled).toBe(true);
    expect(r.body.orderId).toBe('ord-empty');
  });

  it('retries once on 5xx then succeeds', async () => {
    fetchSpy
      .mockResolvedValueOnce(new Response('boom', { status: 503 }))
      .mockResolvedValueOnce(new Response('{"cancelled":true}', { status: 200 }));
    const r = await deleteLimOrder({ orderId: 'ord-retry', apiKey: 'k' });
    expect(r.status).toBe(200);
    expect(r.attempt).toBe(2);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('throws HttpError on 4xx without retry', async () => {
    fetchSpy.mockResolvedValue(new Response('{"error":"bad creds"}', { status: 401 }));
    const err = await deleteLimOrder({ orderId: 'ord-401', apiKey: 'k' }).catch((e) => e);
    expect(err).toBeInstanceOf(HttpError);
    expect((err as HttpError).status).toBe(401);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('aborts before request when circuit-breaker is open', async () => {
    const ckt = vi.fn(() => true);
    await expect(
      deleteLimOrder({ orderId: 'x', apiKey: 'k', circuitOpen: ckt }),
    ).rejects.toThrow(/circuit-breaker open/);
    expect(ckt).toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('reports outcome on success', async () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 204 }));
    const reports: Array<{ ok: boolean; status: number | null }> = [];
    await deleteLimOrder({
      orderId: 'ord-1',
      apiKey: 'k',
      reportOutcome: (ok, status) => reports.push({ ok, status }),
    });
    expect(reports).toEqual([{ ok: true, status: 204 }]);
  });

  it('reports outcome on 5xx retry then success', async () => {
    fetchSpy
      .mockResolvedValueOnce(new Response('boom', { status: 502 }))
      .mockResolvedValueOnce(new Response('{}', { status: 200 }));
    const reports: Array<{ ok: boolean; status: number | null }> = [];
    await deleteLimOrder({
      orderId: 'ord-1',
      apiKey: 'k',
      reportOutcome: (ok, status) => reports.push({ ok, status }),
    });
    expect(reports).toEqual([
      { ok: false, status: 502 },
      { ok: true, status: 200 },
    ]);
  });
});

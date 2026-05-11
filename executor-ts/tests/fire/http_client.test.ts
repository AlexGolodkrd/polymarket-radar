/**
 * Tests for src/lib/http_client.ts — postJson with timeout/retry/circuit.
 *
 * Uses globalThis.fetch stub. Vitest's vi.spyOn on fetch lets us
 * count attempts, return synthetic responses, simulate timeouts.
 */
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { postJson, HttpError } from '../../src/lib/http_client.js';

describe('postJson', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch') as ReturnType<typeof vi.spyOn>;
  });
  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it('returns parsed JSON on 2xx', async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ orderID: 'abc-123', status: 'matched' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const r = await postJson({
      url: 'https://api.example.com/path',
      body: { foo: 'bar' },
    });
    expect(r.status).toBe(200);
    expect((r.body as { orderID: string }).orderID).toBe('abc-123');
    expect(r.attempt).toBe(1);
  });

  it('throws HttpError on 4xx without retry', async () => {
    fetchSpy.mockResolvedValue(
      new Response('{"error":"bad signature"}', { status: 400 }),
    );
    await expect(
      postJson({
        url: 'https://api.example.com/path',
        body: {},
      }),
    ).rejects.toThrow(HttpError);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('retries once on 5xx then succeeds', async () => {
    fetchSpy
      .mockResolvedValueOnce(new Response('boom', { status: 502 }))
      .mockResolvedValueOnce(new Response('{"ok":true}', { status: 200 }));
    const r = await postJson({
      url: 'https://api.example.com/path',
      body: {},
    });
    expect(r.status).toBe(200);
    expect(r.attempt).toBe(2);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('throws on second 5xx after one retry', async () => {
    fetchSpy
      .mockResolvedValueOnce(new Response('boom', { status: 503 }))
      .mockResolvedValueOnce(new Response('still boom', { status: 503 }));
    const err = await postJson({
      url: 'https://api.example.com/path',
      body: {},
    }).catch((e) => e);
    expect(err).toBeInstanceOf(HttpError);
    expect((err as HttpError).status).toBe(503);
    expect((err as HttpError).attempt).toBe(2);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('does NOT retry on 4xx (non-transient)', async () => {
    fetchSpy.mockResolvedValue(
      new Response('{"error":"INVALID_SIGNATURE"}', { status: 401 }),
    );
    await expect(
      postJson({
        url: 'https://api.example.com/path',
        body: {},
      }),
    ).rejects.toThrow(HttpError);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('aborts before request when circuit-breaker is open', async () => {
    const ckt = vi.fn(() => true);
    await expect(
      postJson({
        url: 'https://api.example.com/path',
        body: {},
        circuitOpen: ckt,
      }),
    ).rejects.toThrow(/circuit-breaker open/);
    expect(ckt).toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('reports success outcome to reportOutcome', async () => {
    fetchSpy.mockResolvedValue(new Response('{"ok":true}', { status: 200 }));
    const reports: Array<{ ok: boolean; status: number | null }> = [];
    await postJson({
      url: 'https://api.example.com/path',
      body: {},
      reportOutcome: (ok, status) => reports.push({ ok, status }),
    });
    expect(reports).toEqual([{ ok: true, status: 200 }]);
  });

  it('reports failure outcome on 5xx (both attempts)', async () => {
    fetchSpy
      .mockResolvedValueOnce(new Response('boom', { status: 502 }))
      .mockResolvedValueOnce(new Response('{"ok":true}', { status: 200 }));
    const reports: Array<{ ok: boolean; status: number | null }> = [];
    await postJson({
      url: 'https://api.example.com/path',
      body: {},
      reportOutcome: (ok, status) => reports.push({ ok, status }),
    });
    expect(reports).toEqual([
      { ok: false, status: 502 },
      { ok: true, status: 200 },
    ]);
  });

  it('serializes object body as JSON', async () => {
    let captured: { body?: string } = {};
    fetchSpy.mockImplementation(async (_url, init) => {
      captured = { body: init?.body as string };
      return new Response('{}', { status: 200 });
    });
    await postJson({
      url: 'https://api.example.com/path',
      body: { foo: 'bar', n: 42 },
    });
    expect(captured.body).toBe('{"foo":"bar","n":42}');
  });

  it('passes string body as-is', async () => {
    let captured: { body?: string } = {};
    fetchSpy.mockImplementation(async (_url, init) => {
      captured = { body: init?.body as string };
      return new Response('{}', { status: 200 });
    });
    await postJson({
      url: 'https://api.example.com/path',
      body: 'raw-string-payload',
    });
    expect(captured.body).toBe('raw-string-payload');
  });

  it('sets Content-Type: application/json by default', async () => {
    let captured: { headers?: HeadersInit } = {};
    fetchSpy.mockImplementation(async (_url, init) => {
      captured = { headers: init?.headers };
      return new Response('{}', { status: 200 });
    });
    await postJson({
      url: 'https://api.example.com/path',
      body: { x: 1 },
    });
    const h = new Headers(captured.headers);
    expect(h.get('Content-Type')).toBe('application/json');
  });

  it('merges custom headers with default Content-Type', async () => {
    let captured: { headers?: HeadersInit } = {};
    fetchSpy.mockImplementation(async (_url, init) => {
      captured = { headers: init?.headers };
      return new Response('{}', { status: 200 });
    });
    await postJson({
      url: 'https://api.example.com/path',
      body: {},
      headers: { 'X-API-Key': 'secret-uuid' },
    });
    const h = new Headers(captured.headers);
    expect(h.get('X-API-Key')).toBe('secret-uuid');
    expect(h.get('Content-Type')).toBe('application/json');
  });
});

describe('HttpError', () => {
  it('isTransient true for 5xx', () => {
    const e = new HttpError('x', 503, 'host', '/p', 1);
    expect(e.isTransient()).toBe(true);
  });

  it('isTransient true for null status (network)', () => {
    const e = new HttpError('x', null, 'host', '/p', 1);
    expect(e.isTransient()).toBe(true);
  });

  it('isTransient false for 4xx', () => {
    const e = new HttpError('x', 400, 'host', '/p', 1);
    expect(e.isTransient()).toBe(false);
  });

  it('toJSON includes structured fields', () => {
    const e = new HttpError('boom', 502, 'api.example', '/path', 2, 'long body...');
    const j = e.toJSON();
    expect(j.status).toBe(502);
    expect(j.host).toBe('api.example');
    expect(j.path).toBe('/path');
    expect(j.attempt).toBe(2);
  });
});

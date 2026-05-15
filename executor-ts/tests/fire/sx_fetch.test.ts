/**
 * Unit tests for the SX Bet maker-order fetcher. Mocks global fetch so
 * we exercise:
 *  - happy path: response `{data: [order, order]}` → returns 2 orders
 *  - legacy envelope: `{data: {orders: [...]}}` (v25 shape)
 *  - empty book: `{data: []}` → returns []
 *  - non-2xx: throws HttpError
 *  - missing marketHash: throws synchronously
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { fetchSxMakerOrders } from '../../src/fire/sx_fetch.js';
import { HttpError } from '../../src/lib/http_client.js';

const originalFetch = globalThis.fetch;

function mockFetchResponse(status: number, body: unknown): typeof fetch {
  return ((_: string, _opts?: unknown) =>
    Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      text: async () =>
        typeof body === 'string' ? body : JSON.stringify(body),
    } as Response)) as typeof fetch;
}

describe('fetchSxMakerOrders', () => {
  beforeEach(() => {
    globalThis.fetch = originalFetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('parses the current API shape {data: [...]}', async () => {
    const fakeOrders = [
      {
        orderHash: '0xaaa',
        percentageOdds: '5000000000000000000000', // 0.05 in 1e20 units
        isMakerBettingOutcomeOne: true,
        totalBetSize: '1000000', // $1.00 in 6dp wei
        fillAmount: '0',
        orderStatus: 'ACTIVE',
      },
      {
        orderHash: '0xbbb',
        percentageOdds: '8000000000000000000000',
        isMakerBettingOutcomeOne: false,
        totalBetSize: '2000000',
        fillAmount: '0',
        orderStatus: 'ACTIVE',
      },
    ];
    globalThis.fetch = mockFetchResponse(200, { data: fakeOrders });
    const got = await fetchSxMakerOrders({ marketHash: '0xabc123' });
    expect(got.length).toBe(2);
    expect(got[0]!.orderHash).toBe('0xaaa');
  });

  it('parses legacy {data: {orders: [...]}} envelope', async () => {
    const fake = [{ orderHash: '0xccc', percentageOdds: '5e21', isMakerBettingOutcomeOne: true }];
    globalThis.fetch = mockFetchResponse(200, { data: { orders: fake } });
    const got = await fetchSxMakerOrders({ marketHash: '0xabc123' });
    expect(got.length).toBe(1);
    expect(got[0]!.orderHash).toBe('0xccc');
  });

  it('returns empty array on empty book', async () => {
    globalThis.fetch = mockFetchResponse(200, { data: [] });
    const got = await fetchSxMakerOrders({ marketHash: '0xabc123' });
    expect(got).toEqual([]);
  });

  it('throws HttpError on non-2xx', async () => {
    globalThis.fetch = mockFetchResponse(429, { message: 'rate limited' });
    await expect(
      fetchSxMakerOrders({ marketHash: '0xabc' }),
    ).rejects.toThrow(HttpError);
  });

  it('throws synchronously without marketHash', async () => {
    await expect(
      // @ts-expect-error intentionally missing
      fetchSxMakerOrders({}),
    ).rejects.toThrow(/marketHash required/);
  });
});

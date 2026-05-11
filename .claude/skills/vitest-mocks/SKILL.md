---
name: vitest-mocks
description: Patterns for mocking external dependencies in executor-ts vitest tests — viem signers, Socket.IO/WS clients, HTTP fetch (POST /order), fillRegistry, environment variables. Covers the test-seams we built during TS-5 so future changes don't regress them. Use whenever you add a test that touches network, signing keys, or async timing.
---

# Vitest Mocks — the test seams in executor-ts

The TS executor has 4 boundaries with the outside world:
1. **viem signers** — turning private keys into EIP-712 signatures
2. **HTTP `fetch`** — POSTing orders to Polymarket / SX / Limitless
3. **WebSocket / Socket.IO clients** — listening for fill events
4. **Environment** — `process.env`, `Credentials.env`

Every test that exercises code crossing one of these boundaries MUST use a fake — never the real thing. Real signers leak entropy + slow tests; real `fetch` flakes on CI; real WS clients introduce timing nondeterminism; reading the operator's actual Credentials.env in CI is a security disaster.

This skill is the playbook for what to fake, how, and why.

## 1. Mocking viem signers

The real `privateKeyToAccount` does CPU-bound crypto. Tests don't need real signatures — they need DETERMINISTIC byte strings.

```ts
// At top of test file
import { vi } from 'vitest';

vi.mock('viem/accounts', async () => {
  const actual = await vi.importActual<typeof import('viem/accounts')>('viem/accounts');
  return {
    ...actual,
    privateKeyToAccount: vi.fn((pk: string) => ({
      address: ('0x' + pk.slice(2, 42)) as `0x${string}`,
      signTypedData: vi.fn(async () => '0xMOCKSIGNATURE' as const),
      signMessage: vi.fn(async () => '0xMOCKMSGSIG' as const),
    })),
  };
});
```

**Why importActual:** preserves the other exports (`mnemonicToAccount`, etc.) so unrelated code paths don't break.

**Why deterministic mock signature:** lets you assert on the bytes the builder produces (`expect(order.signature).toBe('0xMOCKSIGNATURE')`).

**Gotcha:** if your code calls `account.address` and stores it before `signTypedData`, the mock above already covers it. But if you `account.publicKey`, add it to the returned object — vitest doesn't auto-stub.

## 2. Mocking HTTP fetch (POST /order)

The executor uses node's global `fetch` (Node 20+). Mock it at the module level:

```ts
import { vi } from 'vitest';

const fetchMock = vi.fn();
vi.stubGlobal('fetch', fetchMock);

beforeEach(() => {
  fetchMock.mockReset();
});

test('builders posts order to polymarket', async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => ({ orderID: '0xabc123', status: 'live' }),
    text: async () => '',
  } as Response);

  const result = await postOrder(signedOrder, wallet);
  expect(result.orderId).toBe('0xabc123');
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/order'),
    expect.objectContaining({ method: 'POST' }),
  );
});
```

**Pattern: mock once per test.** Tests that exercise the failure path do `mockRejectedValueOnce(new Error('429 rate limit'))` and assert the retry/backoff logic.

**Anti-pattern: don't use msw.** msw is fine but adds a 200ms server boot. For 100+ unit tests, stick with vi.fn(); reserve msw for integration tests that exercise multiple HTTP hops.

## 3. Mocking Socket.IO / WS

Socket.IO and WS clients have constructor side effects (open a TCP connection). Inject a factory:

```ts
// poly_user_ws.ts production
import { WebSocket } from 'ws';
constructor(opts: { ..., wsFactory?: (url: string) => WebSocket }) {
  this.wsFactory = opts.wsFactory ?? ((url) => new WebSocket(url));
}

// In runForever()
this.ws = this.wsFactory(WS_URL);
```

```ts
// test
class FakeWS extends EventEmitter {
  readyState = 1;
  close = vi.fn();
  send = vi.fn();
}
const fakeWs = new FakeWS();
const wsFactory = vi.fn(() => fakeWs as unknown as WebSocket);

const listener = new PolyUserWS({ wallet, wsFactory });
listener.start();
fakeWs.emit('open');                  // simulate connect
fakeWs.emit('message', JSON.stringify({ event: 'trade_filled', ... }));
// Now assert fillRegistry received the fill
```

**Why an injected factory and not `vi.mock('ws', ...)`:** mocking 'ws' globally breaks tests that legitimately use `WebSocket.OPEN` constant. Factory injection is per-instance and explicit.

## 4. Mocking process.env

Tests should NEVER read the operator's actual Credentials.env. Stub:

```ts
import { vi } from 'vitest';

beforeEach(() => {
  vi.stubEnv('DRY_RUN', '1');
  vi.stubEnv('BOT1_ETH_ADDRESS', '0x' + '1'.repeat(40));
  // DO NOT stub *_PRIVATE_KEY in tests — production code branches on
  // canSign which respects the absence of keys.
});

afterEach(() => {
  vi.unstubAllEnvs();
});
```

**Gotcha:** stubbed env is process-wide. If you test concurrent code that reads env at import time, the stub only applies AFTER import. Use `vi.resetModules()` to force reimport with new env if needed.

## 5. Mocking fillRegistry

For atomic.fireArb tests, the easiest mock is replacing the singleton:

```ts
import * as fillsModule from '../executor/fills.js';

const fakeRegistry = {
  register: vi.fn(() => ({
    promise: Promise.resolve({ filledPrice: 0.42, filledSize: 100, orderId: 'X' }),
    cancel: vi.fn(),
  })),
  consumeByOrderId: vi.fn(() => true),
  consumeBySlug: vi.fn(() => false),
  expireStale: vi.fn(),
  metrics: vi.fn(() => ({ pending: 0, byOrderId: 0, bySlug: 0 })),
};

vi.spyOn(fillsModule, 'registry', 'get').mockReturnValue(fakeRegistry as unknown as typeof fillsModule.registry);
```

For testing TIMEOUT behavior, have the fake return a never-resolving promise plus a `setTimeout` advance via `vi.advanceTimersByTime(6_000)` (with `vi.useFakeTimers()`).

## 6. Fake timers — when (and when not) to use

**Use `vi.useFakeTimers()`** when testing:
- Retry/backoff logic
- Heartbeat intervals
- Dead-man timeouts
- Janitor sweeps

**Don't use fake timers** when:
- Testing real network code (use mocks instead)
- Tests have promise.then chains across timer boundaries (subtle async ordering bugs)

Pattern:
```ts
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test('reconnects after 30s dead-man', async () => {
  const listener = new PolyUserWS({ ..., wsFactory });
  listener.start();
  fakeWs.emit('open');
  fakeWs.emit('message', JSON.stringify({ event: 'sub_ok' }));
  
  await vi.advanceTimersByTimeAsync(95_000);  // > heartbeat timeout
  
  expect(listener.reconnectCount).toBe(1);
});
```

**Why `advanceTimersByTimeAsync`** (not `advanceTimersByTime`): the async version awaits microtasks between timer ticks, so `setImmediate(...)` + promise chains resolve in the same step. Without it, you assert on stale state.

## 7. Asserting on log lines (when you must)

```ts
const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
await fn();
expect(logSpy).toHaveBeenCalledWith(expect.stringContaining('expected text'));
logSpy.mockRestore();
```

Better pattern: have the function take an optional `logger` arg and inject a recording fake. Avoids spying on global console.

## Lessons learned (TS-5 retrospective)

- **Mock the seam, not the unit.** Mocking `fetch` is fine; mocking `BotConnector.placeOrder` defeats the purpose of testing the builder.
- **Real vitest tests should be < 50ms each.** If a test runs > 200ms, you forgot to mock something. Check for accidentally-real fetch.
- **Don't reuse mocks across tests.** Always `mockReset()` in `beforeEach`. Otherwise test order matters → flake.
- **Tests that exercise real signatures belong in a separate `integration/` folder.** Run them in CI under a flag (`pnpm test:integration`), not in the default unit-test loop.

## See also

- `fillregistry-pattern` — what to mock when testing the fill bridge
- `ws-listener-lifecycle` — what to mock when testing WS reconnect
- `eip712-typescript-parity` — signing seam reference
- `pytest-setup` — the Python sibling skill (conftest fixtures, mocking patterns)

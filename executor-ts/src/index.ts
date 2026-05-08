/**
 * executor-ts entry point — Phase TS-1 skeleton.
 *
 * This file exists so `tsx watch src/index.ts` and `npm start` have
 * something to run during early development. It does NOT yet wire in
 * Fastify or the FireRequest endpoint — that lands in Phase TS-3 once
 * the builder layer (this PR + sx + limitless) is fully validated.
 *
 * For now this is a smoke test: imports each builder module and prints
 * a confirmation. If `npm run typecheck` passes and this file runs,
 * the TS toolchain is healthy.
 */

import { buildPolyOrder } from './builders/poly.js';
import { buildSxOrder } from './builders/sx.js';
import { buildLimitlessOrder } from './builders/limitless.js';

async function main(): Promise<void> {
  // Touch each builder import so tree-shakers can't silently drop them.
  // biome-ignore lint/suspicious/noConsoleLog: smoke-test entry
  console.log('executor-ts builders loaded:', {
    poly: typeof buildPolyOrder,
    sx: typeof buildSxOrder,
    limitless: typeof buildLimitlessOrder,
  });
}

main().catch((err: unknown) => {
  console.error(err);
  process.exit(1);
});

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

async function main(): Promise<void> {
  // Touch the import so tree-shakers can't silently drop the module.
  // No private key, no signing — just sanity that the function exists
  // and types compile.
  // biome-ignore lint/suspicious/noConsoleLog: smoke-test entry
  console.log('executor-ts skeleton OK — buildPolyOrder loaded:', typeof buildPolyOrder);
}

main().catch((err: unknown) => {
  console.error(err);
  process.exit(1);
});

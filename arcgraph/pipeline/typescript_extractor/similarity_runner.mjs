import { readStdin } from './runtime.mjs';
import { scorePersistedSimilarityProfiles } from './similarity.mjs';

async function main() {
  const input = JSON.parse(await readStdin());
  // An omitted allowlist means "no filtering" (restoredProfiles' null), not
  // "drop every repo-local target" (an empty array) — the two are opposites,
  // so the absent-field default must not be spelled [].
  const result = scorePersistedSimilarityProfiles(input.profiles || [], {
    validTargetIds: input.validTargetIds ?? null,
    moduleNames: input.moduleNames ?? null,
  });
  process.stdout.write(JSON.stringify(result));
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message || error}\n`);
  process.exit(1);
});

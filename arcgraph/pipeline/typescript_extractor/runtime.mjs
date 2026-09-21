import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);

export function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => {
      data += chunk;
    });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

export function loadTypeScript(repoRoot) {
  const candidates = [
    path.join(repoRoot, 'frontend', 'node_modules', 'typescript'),
    path.join(repoRoot, 'node_modules', 'typescript'),
    ...bundledWorkspaceTypeScriptCandidates(),
    'typescript',
  ];
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch {
      // Try the next conventional location.
    }
  }
  throw new Error(
    'Cannot load the TypeScript compiler API. Run npm install in frontend/ or install typescript.'
  );
}

function bundledWorkspaceTypeScriptCandidates() {
  const candidates = [];
  let current = path.dirname(fileURLToPath(import.meta.url));
  while (true) {
    candidates.push(path.join(current, 'frontend', 'node_modules', 'typescript'));
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return candidates;
}

export function diagnosticMessageText(messageText) {
  if (typeof messageText === 'string') return messageText;
  if (messageText && typeof messageText.messageText !== 'undefined') {
    return diagnosticMessageText(messageText.messageText);
  }
  return String(messageText || 'unknown TypeScript configuration error');
}

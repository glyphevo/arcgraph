import path from 'node:path';
import { inflateSync } from 'node:zlib';

import { readStdin, loadTypeScript } from './typescript_extractor/runtime.mjs';
import { parseSources } from './typescript_extractor/sources.mjs';
import {
  buildFileResolver,
  loadTypeScriptConfig,
} from './typescript_extractor/module_resolution.mjs';
import { collectDeclarations } from './typescript_extractor/declarations.mjs';
import { collectEdges } from './typescript_extractor/edges.mjs';
import { collectSimilarity } from './typescript_extractor/similarity.mjs';
import {
  declarationLineKey,
  normalizePath,
} from './typescript_extractor/graph_primitives.mjs';

function createTypeScriptProgram(ts, repoRoot, files) {
  const config = loadTypeScriptConfig(ts, repoRoot);
  const compilerOptions = {
    ...(config.compilerOptions || {}),
    allowJs: true,
    checkJs: false,
    noEmit: true,
    skipLibCheck: true,
  };
  if (compilerOptions.jsx === undefined) {
    compilerOptions.jsx = ts.JsxEmit?.ReactJSX ?? ts.JsxEmit?.React;
  }
  const rootNames = files.map((file) => path.resolve(file.absPath));
  const program = ts.createProgram(rootNames, compilerOptions);
  const sourceFilesByPath = new Map();
  for (const sourceFile of program.getSourceFiles()) {
    sourceFilesByPath.set(normalizePath(path.resolve(sourceFile.fileName)), sourceFile);
  }
  return {
    program,
    checker: program.getTypeChecker(),
    sourceFilesByPath,
  };
}

function addDeclarationContext(declarations, repoRoot, rows) {
  for (const row of rows || []) {
    if (
      !row?.id ||
      !row?.path ||
      !row?.name ||
      !Number.isInteger(row.start_line) ||
      !Number.isInteger(row.end_line)
    ) {
      continue;
    }
    if (row.module) {
      declarations.modulesByDeclarationId.set(row.id, row.module);
      if (!declarations.localSymbols.has(row.module)) {
        declarations.localSymbols.set(row.module, new Map());
      }
      const localSymbols = declarations.localSymbols.get(row.module);
      for (const localName of row.local_names || [row.name]) {
        if (!localSymbols.has(localName)) {
          localSymbols.set(localName, row.id);
        } else if (localSymbols.get(localName) !== row.id) {
          localSymbols.set(localName, null);
        }
      }
    }
    const paths = new Set([
      normalizePath(row.path),
      normalizePath(path.resolve(repoRoot, row.path)),
    ]);
    for (const declarationPath of paths) {
      const key = declarationLineKey(
        declarationPath,
        row.start_line,
        row.end_line,
        row.name
      );
      if (!declarations.declarationLocations.has(key)) {
        declarations.declarationLocations.set(key, row.id);
      } else if (declarations.declarationLocations.get(key) !== row.id) {
        // Same-name declarations can share a line in compact source. A wrong
        // call edge is worse than leaving that genuinely ambiguous target
        // unresolved, so disable only the colliding fallback key.
        declarations.declarationLocations.set(key, null);
      }
    }
  }
}

function restoredContext(input) {
  if (input.contextEncoding !== 'zlib-base64-json-v1') {
    return {
      moduleContext: input.moduleContext || [],
      routes: input.routes || [],
      declarationContext: input.declarationContext || [],
    };
  }
  if (typeof input.contextBundle !== 'string' || input.contextBundle.length === 0) {
    throw new Error('Compressed TypeScript context bundle is missing.');
  }
  const decoded = JSON.parse(
    inflateSync(Buffer.from(input.contextBundle, 'base64')).toString('utf8')
  );
  return {
    moduleContext: decoded.moduleContext || [],
    routes: decoded.routes || [],
    declarationContext: decoded.declarationContext || [],
  };
}

async function main() {
  const input = JSON.parse(await readStdin());
  const context = restoredContext(input);
  const ts = loadTypeScript(input.repoRoot);
  const files = input.files || [];
  const parsed = parseSources(ts, files);
  const compilerContext = createTypeScriptProgram(ts, input.repoRoot, files);
  const resolver = buildFileResolver(ts, input.repoRoot, [
    ...files,
    ...context.moduleContext,
  ]);
  const declarations = collectDeclarations(ts, files, parsed);
  addDeclarationContext(
    declarations,
    input.repoRoot,
    context.declarationContext
  );
  const edgeResult = collectEdges(
    ts,
    files,
    parsed,
    declarations,
    resolver.resolveImport,
    context.routes,
    resolver.warnings,
    compilerContext
  );
  const similarityResult = collectSimilarity(
    ts,
    declarations,
    edgeResult.edges,
    input.similarityProfiles || [],
    { deferScoring: input.deferSimilarityScoring === true }
  );
  // Loop rather than spread: spreading passes every element as a call
  // argument, and a large similarity edge set overflows the V8 argument
  // limit — the same hazard class as recursive AST walks.
  for (const edge of similarityResult.edges) {
    edgeResult.edges.push(edge);
  }
  for (const warning of similarityResult.warnings) {
    edgeResult.warnings.push(warning);
  }
  const nodes = [
    ...declarations.nodes,
    ...(edgeResult.config_nodes || []),
    ...(edgeResult.graph_nodes || []),
  ].map((node) => {
    const copy = { ...node };
    delete copy._moduleId;
    return copy;
  });
  const output = { nodes, ...edgeResult };
  // Only the deferred-scoring incremental path consumes the top-level profile
  // rows; every current profile is already carried on node.properties, so
  // emitting them unconditionally would serialize each one twice.
  if (input.deferSimilarityScoring === true) {
    output.similarity_profiles = similarityResult.profiles || [];
  }
  process.stdout.write(JSON.stringify(output));
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message || error}\n`);
  process.exit(1);
});

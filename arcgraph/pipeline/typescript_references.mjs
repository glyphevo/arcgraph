import path from 'node:path';

import { loadTypeScript, readStdin } from './typescript_extractor/runtime.mjs';

function inside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative !== '' && !relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative);
}

function compilerOptions(ts, repoRoot) {
  const configPath = ts.findConfigFile(repoRoot, ts.sys.fileExists);
  if (!configPath) {
    return {
      allowJs: true,
      checkJs: false,
      module: ts.ModuleKind.NodeNext,
      moduleResolution: ts.ModuleResolutionKind.NodeNext,
      target: ts.ScriptTarget.ESNext,
    };
  }
  const read = ts.readConfigFile(configPath, ts.sys.readFile);
  if (read.error) return { allowJs: true, checkJs: false };
  return ts.parseJsonConfigFileContent(read.config, ts.sys, path.dirname(configPath)).options;
}

function targetPosition(ts, service, fileName, startLine, name) {
  const source = service.getProgram()?.getSourceFile(fileName);
  if (!source) return null;
  const targetLine = Math.max(
    0,
    Math.min(
      Number(startLine || 1) - 1,
      source.getLineAndCharacterOfPosition(source.end).line
    )
  );
  const isDeclaration = (node) =>
    ts.isFunctionDeclaration(node) ||
    ts.isClassDeclaration(node) ||
    ts.isInterfaceDeclaration(node) ||
    ts.isTypeAliasDeclaration(node) ||
    ts.isEnumDeclaration(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node) ||
    ts.isPropertyDeclaration(node) ||
    ts.isVariableDeclaration(node);
  const candidates = [];
  const visit = (node) => {
    const declarationName = isDeclaration(node) ? node.name : null;
    if (declarationName?.getText(source) === String(name)) {
      const position = declarationName.getStart(source);
      const line = source.getLineAndCharacterOfPosition(position).line;
      if (Math.abs(line - targetLine) <= 3) {
        candidates.push({ position, distance: Math.abs(line - targetLine) });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  candidates.sort((left, right) =>
    left.distance - right.distance || left.position - right.position
  );
  return candidates[0]?.position ?? null;
}

function location(ts, repoRoot, service, reference) {
  const fileName = path.resolve(reference.fileName);
  if (!inside(repoRoot, fileName)) return null;
  const source = service.getProgram()?.getSourceFile(fileName);
  const text = source?.text ?? ts.sys.readFile(fileName);
  if (typeof text !== 'string') return null;
  const parsed = source ?? ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true);
  const loc = parsed.getLineAndCharacterOfPosition(reference.textSpan.start);
  const end = parsed.getLineAndCharacterOfPosition(reference.textSpan.start + reference.textSpan.length);
  return {
    path: path.relative(repoRoot, fileName).replaceAll('\\', '/'),
    line: loc.line + 1,
    column: loc.character + 1,
    end_line: end.line + 1,
    end_column: end.character + 1,
    is_definition: Boolean(reference.isDefinition),
    is_write: Boolean(reference.isWriteAccess),
  };
}

async function main() {
  const input = JSON.parse(await readStdin());
  const repoRoot = path.resolve(input.repo_root);
  const ts = loadTypeScript(repoRoot);
  const fileNames = [...new Set((input.files || []).map((value) => path.resolve(repoRoot, value)))]
    .filter((value) => inside(repoRoot, value) && ts.sys.fileExists(value));
  const versions = new Map(fileNames.map((fileName) => [fileName, '0']));
  const options = compilerOptions(ts, repoRoot);
  const host = {
    getScriptFileNames: () => fileNames,
    getScriptVersion: (fileName) => versions.get(path.resolve(fileName)) || '0',
    getScriptSnapshot: (fileName) => {
      const text = ts.sys.readFile(fileName);
      return typeof text === 'string' ? ts.ScriptSnapshot.fromString(text) : undefined;
    },
    getCurrentDirectory: () => repoRoot,
    getCompilationSettings: () => options,
    getDefaultLibFileName: (value) => ts.getDefaultLibFilePath(value),
    fileExists: ts.sys.fileExists,
    readFile: ts.sys.readFile,
    readDirectory: ts.sys.readDirectory,
    directoryExists: ts.sys.directoryExists,
    getDirectories: ts.sys.getDirectories,
  };
  const service = ts.createLanguageService(host, ts.createDocumentRegistry());
  const targetFile = path.resolve(repoRoot, input.target.path);
  if (!inside(repoRoot, targetFile) || !fileNames.includes(targetFile)) {
    return { status: 'unavailable', reason: 'Target file is outside the indexed TypeScript file set.' };
  }
  const position = targetPosition(
    ts,
    service,
    targetFile,
    input.target.start_line,
    input.target.name
  );
  if (position === null) {
    return {
      status: 'unavailable',
      reason: 'The exact target declaration could not be located at its indexed coordinates.',
    };
  }
  const groups = service.findReferences(targetFile, position) || [];
  const raw = groups.flatMap((group) => group.references || []);
  if (raw.length === 0) {
    for (const reference of service.getReferencesAtPosition(targetFile, position) || []) {
      raw.push(reference);
    }
  }
  const byLocation = new Map();
  for (const reference of raw) {
    const item = location(ts, repoRoot, service, reference);
    if (!item) continue;
    const key = `${item.path}:${item.line}:${item.column}:${item.end_line}:${item.end_column}`;
    byLocation.set(key, item);
  }
  const references = [...byLocation.values()].sort((left, right) => {
    // Codepoint order, not localeCompare: the SCIP backend sorts in Python
    // and the truncated subset must be identical on every machine and ICU
    // build.
    if (left.path !== right.path) return left.path < right.path ? -1 : 1;
    return left.line - right.line || left.column - right.column;
  });
  return {
    status: 'available',
    backend: 'typescript_language_service',
    references,
    summary: {
      total: references.length,
      definitions: references.filter((item) => item.is_definition).length,
      writes: references.filter((item) => item.is_write).length,
    },
  };
}

try {
  process.stdout.write(`${JSON.stringify(await main())}\n`);
} catch (error) {
  process.stdout.write(
    `${JSON.stringify({ status: 'unavailable', reason: String(error?.message || error) })}\n`
  );
}

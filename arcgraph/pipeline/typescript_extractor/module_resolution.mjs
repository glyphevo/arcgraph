import fs from 'node:fs';
import path from 'node:path';

import { diagnosticMessageText } from './runtime.mjs';
import {
  externalPackageName,
  normalizePath,
  truncateText,
  walkPreOrder,
} from './graph_primitives.mjs';

function resolutionCandidates(base) {
  const runtimeExtension = path.extname(base).toLowerCase();
  // Prefer only the exact runtime extension the import requested. Adjacent
  // runtime lanes (for example `.jsx` beside an omitted emit `.js`) must stay
  // after the historical TypeScript substitutions so `import "./shim.js"` still
  // resolves to `shim.ts` when the scanner dropped the emit artifact.
  const extensionSubstitutions = {
    '.js': ['.js', '.ts', '.tsx', '.d.ts', '.jsx'],
    '.jsx': ['.jsx', '.tsx', '.ts', '.d.ts', '.js'],
    '.mjs': ['.mjs', '.mts', '.d.mts'],
    '.cjs': ['.cjs', '.cts', '.d.cts'],
  };
  const substitutions = extensionSubstitutions[runtimeExtension] || [];
  const stem = substitutions.length > 0
    ? base.slice(0, -runtimeExtension.length)
    : base;
  // An explicit TypeScript source extension names an exact file
  // (allowImportingTsExtensions); the exact path must win over derived
  // candidates such as `${base}.ts` matching a coexisting `foo.ts.ts`.
  const explicitTsSourceExtensions = new Set(['.ts', '.tsx', '.mts', '.cts']);
  const candidates = [
    ...(explicitTsSourceExtensions.has(runtimeExtension) ? [base] : []),
    ...substitutions.map((extension) => `${stem}${extension}`),
    `${base}.ts`,
    `${base}.tsx`,
    `${base}.d.ts`,
    `${base}.js`,
    `${base}.jsx`,
    `${base}.mts`,
    `${base}.d.mts`,
    `${base}.cts`,
    `${base}.d.cts`,
    `${base}.mjs`,
    `${base}.cjs`,
    path.join(base, 'index.ts'),
    path.join(base, 'index.tsx'),
    path.join(base, 'index.d.ts'),
    path.join(base, 'index.js'),
    path.join(base, 'index.jsx'),
    path.join(base, 'index.mts'),
    path.join(base, 'index.d.mts'),
    path.join(base, 'index.cts'),
    path.join(base, 'index.d.cts'),
    path.join(base, 'index.mjs'),
    path.join(base, 'index.cjs'),
    // The bare base serves exact-extension imports plus the extensionless and
    // directory aliases of the file map. Those aliases are single-valued (the
    // last lane scanned wins), so the bare base is strictly a LAST resort:
    // every per-extension file candidate and every explicit index candidate
    // must be tried first, or `./foo` and `./pkg` resolve by scan order
    // instead of TypeScript's `.ts`-before-`.tsx` priority when two lanes of
    // one stem coexist.
    base,
  ].map((candidate) => normalizePath(candidate));
  return [...new Set(candidates)];
}

export function loadTypeScriptConfig(ts, repoRoot) {
  const warnings = [];
  for (const configName of ['tsconfig.json', 'jsconfig.json']) {
    const configPath = normalizePath(path.resolve(repoRoot, configName));
    if (!fs.existsSync(configPath)) continue;
    const loaded = ts.readConfigFile(configPath, ts.sys.readFile);
    if (loaded.error) {
      warnings.push({
        kind: 'typescript_config_parse_error',
        path: configName,
        message: `Unable to parse ${configName}: ${diagnosticMessageText(loaded.error.messageText)}`,
      });
      return { compilerOptions: {}, configDir: repoRoot, warnings };
    }
    const parsed = ts.parseJsonConfigFileContent(
      loaded.config || {},
      ts.sys,
      path.dirname(configPath),
      {},
      configPath
    );
    if (parsed.errors && parsed.errors.length > 0) {
      warnings.push({
        kind: 'typescript_config_parse_error',
        path: configName,
        message: `Unable to resolve ${configName}: ${parsed.errors
          .map((error) => diagnosticMessageText(error.messageText))
          .join('; ')}`,
      });
      return { compilerOptions: {}, configDir: repoRoot, warnings };
    }
    return {
      compilerOptions: parsed.options || {},
      configDir: path.dirname(configPath),
      warnings,
    };
  }
  return { compilerOptions: {}, configDir: repoRoot, warnings };
}

function matchPathPattern(specifier, pattern) {
  const starIndex = pattern.indexOf('*');
  if (starIndex === -1) {
    return specifier === pattern ? '' : null;
  }
  const prefix = pattern.slice(0, starIndex);
  const suffix = pattern.slice(starIndex + 1);
  if (!specifier.startsWith(prefix) || !specifier.endsWith(suffix)) return null;
  return specifier.slice(prefix.length, specifier.length - suffix.length);
}

function applyPathTarget(target, wildcard) {
  return target.includes('*') ? target.replaceAll('*', wildcard || '') : target;
}

function hasSingleWildcard(value) {
  return value.split('*').length === 2;
}

function uniqueFiles(files) {
  const byPath = new Map();
  for (const file of files) {
    byPath.set(normalizePath(path.resolve(file.absPath)), file);
  }
  return [...byPath.values()];
}

function loadPackageMetadata(repoRoot) {
  const packagePath = path.resolve(repoRoot, 'package.json');
  if (!fs.existsSync(packagePath)) return { packageJson: null, warnings: [] };
  try {
    return {
      packageJson: JSON.parse(fs.readFileSync(packagePath, 'utf8')),
      warnings: [],
    };
  } catch (error) {
    return {
      packageJson: null,
      warnings: [
        {
          kind: 'typescript_package_json_parse_error',
          path: 'package.json',
          message: `Unable to parse package.json: ${error.message || error}`,
        },
      ],
    };
  }
}

function workspacePatterns(packageJson) {
  if (!packageJson || typeof packageJson !== 'object') return [];
  if (Array.isArray(packageJson.workspaces)) return packageJson.workspaces;
  if (
    packageJson.workspaces &&
    typeof packageJson.workspaces === 'object' &&
    Array.isArray(packageJson.workspaces.packages)
  ) {
    return packageJson.workspaces.packages;
  }
  return [];
}

function expandWorkspacePattern(repoRoot, pattern) {
  if (path.isAbsolute(pattern)) return null;
  const parts = normalizePath(pattern).split('/').filter(Boolean);
  if (parts.includes('..')) return null;
  const rootAbs = normalizePath(path.resolve(repoRoot));
  let dirs = [rootAbs];
  for (const part of parts) {
    if (part.includes('*') && part !== '*') return null;
    const nextDirs = [];
    for (const dir of dirs) {
      if (part === '*') {
        if (!fs.existsSync(dir)) continue;
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
          if (entry.isDirectory()) {
            nextDirs.push(normalizePath(path.join(dir, entry.name)));
          }
        }
      } else {
        const candidate = normalizePath(path.join(dir, part));
        if (
          (candidate === rootAbs || candidate.startsWith(`${rootAbs}/`)) &&
          fs.existsSync(candidate) &&
          fs.statSync(candidate).isDirectory()
        ) {
          nextDirs.push(candidate);
        }
      }
    }
    dirs = nextDirs;
  }
  return dirs;
}

function loadWorkspacePackages(repoRoot) {
  const rootMetadata = loadPackageMetadata(repoRoot);
  const warnings = [...rootMetadata.warnings];
  const byName = new Map();
  const rootPackage =
    rootMetadata.packageJson && typeof rootMetadata.packageJson === 'object'
      ? rootMetadata.packageJson
      : {};

  for (const rawPattern of workspacePatterns(rootPackage)) {
    if (typeof rawPattern !== 'string') {
      warnings.push({
        kind: 'typescript_workspace_package_unsupported',
        path: 'package.json',
        message:
          'Unsupported TypeScript workspace package entry; only static string patterns are resolved.',
      });
      continue;
    }
    const packageDirs = expandWorkspacePattern(repoRoot, rawPattern);
    if (!packageDirs) {
      warnings.push({
        kind: 'typescript_workspace_package_unsupported',
        path: 'package.json',
        message: `Unsupported TypeScript workspace package pattern ${rawPattern}; only literal path segments and '*' segments are resolved.`,
      });
      continue;
    }
    for (const packageDir of packageDirs) {
      if (!fs.existsSync(path.join(packageDir, 'package.json'))) continue;
      const metadata = loadPackageMetadata(packageDir);
      warnings.push(...metadata.warnings);
      const packageJson =
        metadata.packageJson && typeof metadata.packageJson === 'object'
          ? metadata.packageJson
          : {};
      const name = typeof packageJson.name === 'string' ? packageJson.name : '';
      if (!name) continue;
      if (!byName.has(name)) byName.set(name, []);
      byName.get(name).push({ dir: packageDir, packageJson });
    }
  }

  return { byName, warnings };
}

const PACKAGE_RUNTIME_CONDITION_ORDER = [
  'import',
  'module',
  'browser',
  'node',
  'development',
  'production',
  'default',
];

function objectTargetValue(target) {
  if (!target || Array.isArray(target) || typeof target !== 'object') return null;
  for (const condition of PACKAGE_RUNTIME_CONDITION_ORDER) {
    if (!Object.prototype.hasOwnProperty.call(target, condition)) continue;
    const value = target[condition];
    if (typeof value === 'string') return value;
    const nestedValue = objectTargetValue(value);
    if (nestedValue) return nestedValue;
  }
  return null;
}

function propertyNameText(ts, name) {
  if (!name) return null;
  if (ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNumericLiteral(name)) {
    return name.text;
  }
  return null;
}

function stringLiteralText(ts, node) {
  if (!node) return null;
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    return node.text;
  }
  return null;
}

function isImportMetaUrl(ts, node) {
  return (
    ts.isPropertyAccessExpression(node) &&
    node.name.text === 'url' &&
    ts.isMetaProperty(node.expression) &&
    node.expression.keywordToken === ts.SyntaxKind.ImportKeyword &&
    node.expression.name.text === 'meta'
  );
}

function staticUrlPath(ts, node, repoRoot) {
  if (
    !ts.isNewExpression(node) ||
    !ts.isIdentifier(node.expression) ||
    node.expression.text !== 'URL'
  ) {
    return null;
  }
  const args = node.arguments || [];
  const rawPath = stringLiteralText(ts, args[0]);
  if (!rawPath || !isImportMetaUrl(ts, args[1])) return null;
  return normalizePath(path.resolve(repoRoot, rawPath));
}

function staticReplacementPath(ts, node, repoRoot) {
  const literal = stringLiteralText(ts, node);
  if (literal !== null) return normalizePath(path.resolve(repoRoot, literal));

  const urlPath = staticUrlPath(ts, node, repoRoot);
  if (urlPath) return urlPath;

  if (!ts.isCallExpression(node)) return null;

  if (ts.isIdentifier(node.expression) && node.expression.text === 'fileURLToPath') {
    const first = node.arguments?.[0];
    return first ? staticUrlPath(ts, first, repoRoot) : null;
  }

  if (
    ts.isPropertyAccessExpression(node.expression) &&
    node.expression.name.text === 'resolve'
  ) {
    const args = [...(node.arguments || [])];
    if (
      args.length >= 2 &&
      ts.isIdentifier(args[0]) &&
      args[0].text === '__dirname'
    ) {
      const parts = args.slice(1).map((arg) => stringLiteralText(ts, arg));
      if (parts.every((part) => part !== null)) {
        return normalizePath(path.resolve(repoRoot, ...parts));
      }
    }
  }

  return null;
}

function matchesAlias(specifier, alias) {
  if (specifier === alias.find) return '';
  return specifier.startsWith(`${alias.find}/`)
    ? specifier.slice(alias.find.length + 1)
    : null;
}

function loadBundlerAliases(ts, repoRoot) {
  const warnings = [];
  const aliases = [];
  const unsupportedAliases = [];
  const configNames = [
    'vite.config.ts',
    'vite.config.js',
    'vite.config.mts',
    'vite.config.mjs',
    'webpack.config.js',
    'webpack.config.cjs',
    'webpack.config.mjs',
  ];

  function warn(configName, message) {
    warnings.push({
      kind: 'typescript_bundler_alias_unsupported',
      path: configName,
      message,
    });
  }

  function unsupportedFind(tsNode) {
    if (!tsNode) return { find: null, label: 'unknown alias' };
    if (tsNode.kind === ts.SyntaxKind.RegularExpressionLiteral) {
      return { find: null, label: 'regex alias' };
    }
    const literal = stringLiteralText(ts, tsNode);
    if (literal !== null) return { find: literal, label: literal };
    return { find: null, label: truncateText(tsNode.getText(), 80) };
  }

  function addAlias(configName, find, replacementNode) {
    const replacement = staticReplacementPath(ts, replacementNode, repoRoot);
    if (replacement) {
      aliases.push({ find, replacement, configName });
      return;
    }
    unsupportedAliases.push({ find, configName });
    warn(
      configName,
      `Unsupported TypeScript bundler alias ${find}; only static string/path aliases are resolved.`
    );
  }

  function collectAliasArray(configName, aliasArray) {
    for (const element of aliasArray.elements) {
      if (!ts.isObjectLiteralExpression(element)) {
        warn(
          configName,
          'Unsupported TypeScript bundler alias entry; only static object entries are resolved.'
        );
        continue;
      }
      let findNode = null;
      let replacementNode = null;
      for (const prop of element.properties) {
        if (!ts.isPropertyAssignment(prop)) continue;
        const name = propertyNameText(ts, prop.name);
        if (name === 'find') findNode = prop.initializer;
        if (name === 'replacement') replacementNode = prop.initializer;
      }
      const find = stringLiteralText(ts, findNode);
      if (!find || !replacementNode) {
        const unsupported = unsupportedFind(findNode);
        if (unsupported.find) unsupportedAliases.push({ find: unsupported.find, configName });
        warn(
          configName,
          `Unsupported TypeScript bundler alias ${unsupported.label}; only static string/path aliases are resolved.`
        );
        continue;
      }
      addAlias(configName, find, replacementNode);
    }
  }

  function collectAliasObject(configName, aliasObject) {
    for (const prop of aliasObject.properties) {
      if (!ts.isPropertyAssignment(prop)) continue;
      const find = propertyNameText(ts, prop.name);
      if (!find) {
        warn(
          configName,
          'Unsupported TypeScript bundler alias key; only static string keys are resolved.'
        );
        continue;
      }
      addAlias(configName, find, prop.initializer);
    }
  }

  function collectAliasInitializer(configName, initializer) {
    if (ts.isObjectLiteralExpression(initializer)) {
      collectAliasObject(configName, initializer);
      return;
    }
    if (ts.isArrayLiteralExpression(initializer)) {
      collectAliasArray(configName, initializer);
      return;
    }
    warn(
      configName,
      'Unsupported TypeScript bundler resolve.alias shape; only object and array aliases are resolved.'
    );
  }

  function visitConfig(configName, root) {
    walkPreOrder(ts, root, (node) => {
      if (
        ts.isPropertyAssignment(node) &&
        propertyNameText(ts, node.name) === 'resolve'
      ) {
        const initializer = node.initializer;
        if (ts.isObjectLiteralExpression(initializer)) {
          for (const prop of initializer.properties) {
            if (
              ts.isPropertyAssignment(prop) &&
              propertyNameText(ts, prop.name) === 'alias'
            ) {
              collectAliasInitializer(configName, prop.initializer);
            }
          }
        }
      }
    });
  }

  for (const configName of configNames) {
    const configPath = path.resolve(repoRoot, configName);
    if (!fs.existsSync(configPath)) continue;
    const sourceText = fs.readFileSync(configPath, 'utf8');
    const scriptKind = configName.endsWith('.ts') || configName.endsWith('.mts')
      ? ts.ScriptKind.TS
      : ts.ScriptKind.JS;
    const sourceFile = ts.createSourceFile(
      configName,
      sourceText,
      ts.ScriptTarget.Latest,
      true,
      scriptKind
    );
    visitConfig(configName, sourceFile);
  }

  return { aliases, unsupportedAliases, warnings };
}

export function buildFileResolver(ts, repoRoot, files) {
  // Exact absolute paths are written first and are never overwritten: a
  // derived alias of one file (the extensionless stem of `foo.ts.ts` is
  // `foo.ts`) must not shadow another file's real path. Derived aliases fill
  // empty slots only; priority among files sharing an alias is decided by
  // the resolver's candidate order, which queries exact paths first.
  const byAbs = new Map();
  for (const file of files) {
    byAbs.set(normalizePath(path.resolve(file.absPath)), file);
  }
  for (const file of files) {
    const abs = normalizePath(path.resolve(file.absPath));
    const extensionless = abs.endsWith('.d.ts')
      ? abs.slice(0, -'.d.ts'.length)
      : abs.replace(/\.(tsx?|jsx?)$/, '');
    if (!byAbs.has(extensionless)) byAbs.set(extensionless, file);
    const base = abs.endsWith('/index.d.ts')
      ? abs.slice(0, -'/index.d.ts'.length)
      : abs.replace(/\/index\.(tsx?|jsx?)$/, '');
    if (!byAbs.has(base)) byAbs.set(base, file);
  }
  const tsConfig = loadTypeScriptConfig(ts, repoRoot);
  const compilerOptions = tsConfig.compilerOptions || {};
  const paths =
    compilerOptions.paths && typeof compilerOptions.paths === 'object'
      ? compilerOptions.paths
      : {};
  const hasPaths = Object.keys(paths).length > 0;
  const baseUrl =
    typeof compilerOptions.baseUrl === 'string'
      ? path.resolve(tsConfig.configDir, compilerOptions.baseUrl)
      : null;
  const pathsBase = baseUrl || tsConfig.configDir;
  const bundlerAliases = loadBundlerAliases(ts, repoRoot);
  const workspacePackages = loadWorkspacePackages(repoRoot);
  const packageMetadataByDir = new Map();
  const packageWarnings = [...workspacePackages.warnings];
  const repoRootAbs = normalizePath(path.resolve(repoRoot));

  function packageMetadataFor(startDir) {
    let dir = normalizePath(path.resolve(startDir));
    while (dir.startsWith(repoRootAbs)) {
      if (packageMetadataByDir.has(dir)) return packageMetadataByDir.get(dir);
      if (fs.existsSync(path.join(dir, 'package.json'))) {
        const metadata = loadPackageMetadata(dir);
        packageMetadataByDir.set(dir, metadata);
        packageWarnings.push(...metadata.warnings);
        return metadata;
      }
      const parent = normalizePath(path.dirname(dir));
      if (parent === dir) break;
      dir = parent;
    }
    return { packageJson: null, warnings: [] };
  }

  for (const file of files) {
    packageMetadataFor(path.dirname(file.absPath));
  }

  function resolveBase(base) {
    for (const candidate of resolutionCandidates(base)) {
      const match = byAbs.get(candidate);
      if (match) return match;
    }
    return null;
  }

  function resolvePathAlias(specifier) {
    if (!hasPaths) return null;
    let matchedPattern = false;
    const matches = [];
    for (const [pattern, rawTargets] of Object.entries(paths)) {
      const wildcard = matchPathPattern(specifier, pattern);
      if (wildcard === null) continue;
      matchedPattern = true;
      const targets = Array.isArray(rawTargets) ? rawTargets : [];
      for (const rawTarget of targets) {
        if (typeof rawTarget !== 'string') continue;
        const resolved = resolveBase(
          path.resolve(pathsBase, applyPathTarget(rawTarget, wildcard))
        );
        if (resolved) matches.push(resolved);
      }
    }
    const unique = uniqueFiles(matches);
    if (unique.length === 1) return { kind: 'module', file: unique[0] };
    if (unique.length > 1) {
      return {
        kind: 'unresolved',
        specifier,
        reason: 'alias_ambiguous',
        candidates: unique.map((file) => `mod:${file.module}`).sort(),
      };
    }
    if (matchedPattern) {
      return { kind: 'unresolved', specifier, reason: 'alias_unresolved' };
    }
    return null;
  }

  function resolveBundlerAlias(specifier) {
    let matchedAlias = false;
    const matches = [];
    for (const alias of bundlerAliases.aliases) {
      const suffix = matchesAlias(specifier, alias);
      if (suffix === null) continue;
      matchedAlias = true;
      const resolved = resolveBase(
        suffix ? path.join(alias.replacement, suffix) : alias.replacement
      );
      if (resolved) matches.push(resolved);
    }
    const unique = uniqueFiles(matches);
    if (unique.length === 1) return { kind: 'module', file: unique[0] };
    if (unique.length > 1) {
      return {
        kind: 'unresolved',
        specifier,
        reason: 'bundler_alias_ambiguous',
        candidates: unique.map((file) => `mod:${file.module}`).sort(),
      };
    }
    if (matchedAlias) {
      return { kind: 'unresolved', specifier, reason: 'bundler_alias_unresolved' };
    }
    for (const alias of bundlerAliases.unsupportedAliases) {
      if (matchesAlias(specifier, alias) !== null) {
        return { kind: 'unresolved', specifier, reason: 'bundler_alias_unresolved' };
      }
    }
    return null;
  }

  function targetCandidates(target, wildcard) {
    const candidates = [];
    if (typeof target === 'string') {
      candidates.push(target);
    } else if (Array.isArray(target)) {
      for (const item of target) {
        if (typeof item === 'string') candidates.push(item);
        else if (objectTargetValue(item)) candidates.push(objectTargetValue(item));
      }
    } else {
      const value = objectTargetValue(target);
      if (value) candidates.push(value);
    }
    return candidates.map((candidate) => applyPathTarget(candidate, wildcard));
  }

  function resolvePackageTarget(baseDir, specifier, target, wildcard, reasonPrefix) {
    const candidates = targetCandidates(target, wildcard);
    const matches = [];
    for (const candidate of candidates) {
      const resolved = resolveBase(path.resolve(baseDir, candidate));
      if (resolved) matches.push(resolved);
    }
    const unique = uniqueFiles(matches);
    if (!Array.isArray(target) && unique.length === 1) {
      return { kind: 'module', file: unique[0] };
    }
    if (Array.isArray(target) || unique.length > 1) {
      return {
        kind: 'unresolved',
        specifier,
        reason: `${reasonPrefix}_ambiguous`,
        candidates: unique.map((file) => `mod:${file.module}`).sort(),
      };
    }
    return {
      kind: 'unresolved',
      specifier,
      reason: `${reasonPrefix}_unresolved`,
    };
  }

  function matchPackageMap(specifier, mapping, packageDir, reasonPrefix) {
    if (!mapping || typeof mapping !== 'object') return null;
    if (Object.prototype.hasOwnProperty.call(mapping, specifier)) {
      return resolvePackageTarget(
        packageDir,
        specifier,
        mapping[specifier],
        '',
        reasonPrefix
      );
    }
    let matchedPattern = false;
    const matches = [];
    for (const [pattern, target] of Object.entries(mapping)) {
      if (!hasSingleWildcard(pattern)) continue;
      const wildcard = matchPathPattern(specifier, pattern);
      if (wildcard === null) continue;
      matchedPattern = true;
      const resolved = resolvePackageTarget(
        packageDir,
        specifier,
        target,
        wildcard,
        reasonPrefix
      );
      if (resolved.kind === 'module') matches.push(resolved.file);
      else if (resolved.reason === `${reasonPrefix}_ambiguous`) return resolved;
    }
    const unique = uniqueFiles(matches);
    if (unique.length === 1) return { kind: 'module', file: unique[0] };
    if (unique.length > 1) {
      return {
        kind: 'unresolved',
        specifier,
        reason: `${reasonPrefix}_ambiguous`,
        candidates: unique.map((file) => `mod:${file.module}`).sort(),
      };
    }
    if (matchedPattern) {
      return { kind: 'unresolved', specifier, reason: `${reasonPrefix}_unresolved` };
    }
    return null;
  }

  function resolvePackageImport(specifier, importer) {
    const packageDir = normalizePath(path.dirname(importer.absPath));
    const metadata = packageMetadataFor(packageDir);
    const packageJson =
      metadata.packageJson && typeof metadata.packageJson === 'object'
        ? metadata.packageJson
        : {};
    const packageName =
      typeof packageJson.name === 'string' && packageJson.name
        ? packageJson.name
        : null;
    const packageExports =
      packageJson.exports && typeof packageJson.exports === 'object'
        ? packageJson.exports
        : packageJson.exports;
    const packageImports =
      packageJson.imports && typeof packageJson.imports === 'object'
        ? packageJson.imports
        : null;
    const packageRoot = (() => {
      let dir = normalizePath(path.resolve(path.dirname(importer.absPath)));
      while (dir.startsWith(repoRootAbs)) {
        if (fs.existsSync(path.join(dir, 'package.json'))) return dir;
        const parent = normalizePath(path.dirname(dir));
        if (parent === dir) break;
        dir = parent;
      }
      return repoRoot;
    })();
    if (specifier.startsWith('#')) {
      return (
        matchPackageMap(specifier, packageImports, packageRoot, 'package_imports') || {
          kind: 'unresolved',
          specifier,
          reason: 'package_imports_unresolved',
        }
      );
    }
    if (!packageName) return null;
    let exportKey = null;
    if (specifier === packageName) {
      exportKey = '.';
    } else if (specifier.startsWith(`${packageName}/`)) {
      exportKey = `./${specifier.slice(packageName.length + 1)}`;
    }
    if (!exportKey) return null;
    if (typeof packageExports === 'string' && exportKey === '.') {
      return resolvePackageTarget(
        packageRoot,
        specifier,
        packageExports,
        '',
        'package_exports'
      );
    }
    return (
      matchPackageMap(exportKey, packageExports, packageRoot, 'package_exports') || {
        kind: 'unresolved',
        specifier,
        reason: 'package_exports_unresolved',
      }
    );
  }

  function resolveWorkspacePackageImport(specifier) {
    const packageName = externalPackageName(specifier);
    const packages = workspacePackages.byName.get(packageName) || [];
    if (packages.length === 0) return null;
    let exportKey = null;
    if (specifier === packageName) {
      exportKey = '.';
    } else if (specifier.startsWith(`${packageName}/`)) {
      exportKey = `./${specifier.slice(packageName.length + 1)}`;
    }
    if (!exportKey) return null;

    const matches = [];
    for (const workspacePackage of packages) {
      const packageJson = workspacePackage.packageJson || {};
      const packageExports =
        packageJson.exports && typeof packageJson.exports === 'object'
          ? packageJson.exports
          : packageJson.exports;
      const resolved =
        typeof packageExports === 'string' && exportKey === '.'
          ? resolvePackageTarget(
              workspacePackage.dir,
              specifier,
              packageExports,
              '',
              'workspace_package_exports'
            )
          : matchPackageMap(
              exportKey,
              packageExports,
              workspacePackage.dir,
              'workspace_package_exports'
            ) || {
              kind: 'unresolved',
              specifier,
              reason: 'workspace_package_exports_unresolved',
            };
      if (resolved.kind === 'module') {
        matches.push(resolved.file);
      } else if (resolved.reason === 'workspace_package_exports_ambiguous') {
        return resolved;
      }
    }

    const unique = uniqueFiles(matches);
    if (packages.length > 1 || unique.length > 1) {
      return {
        kind: 'unresolved',
        specifier,
        reason: 'workspace_package_exports_ambiguous',
        candidates: unique.map((file) => `mod:${file.module}`).sort(),
      };
    }
    if (unique.length === 1) return { kind: 'module', file: unique[0] };
    return {
      kind: 'unresolved',
      specifier,
      reason: 'workspace_package_exports_unresolved',
    };
  }

  function resolveImport(specifier, importer) {
    if (specifier.startsWith('.')) {
      const match = resolveBase(path.resolve(path.dirname(importer.absPath), specifier));
      if (match) return { kind: 'module', file: match };
      return { kind: 'unresolved', specifier };
    }

    const aliasMatch = resolvePathAlias(specifier);
    if (aliasMatch) return aliasMatch;

    const bundlerAliasMatch = resolveBundlerAlias(specifier);
    if (bundlerAliasMatch) return bundlerAliasMatch;

    if (baseUrl) {
      const match = resolveBase(path.resolve(baseUrl, specifier));
      if (match) return { kind: 'module', file: match };
    }

    const packageMatch = resolvePackageImport(specifier, importer);
    if (packageMatch) return packageMatch;

    const workspacePackageMatch = resolveWorkspacePackageImport(specifier);
    if (workspacePackageMatch) return workspacePackageMatch;

    return { kind: 'external', package: externalPackageName(specifier), specifier };
  }

  return {
    resolveImport,
    warnings: [...tsConfig.warnings, ...bundlerAliases.warnings, ...packageWarnings],
  };
}

import crypto from 'node:crypto';

import {
  declarationLineKey,
  evidence,
  isComponentName,
  isHookName,
  lineInfo,
  makeConfigNode,
  makeEdge,
  normalizePath,
  truncateText,
  walkPreOrder,
} from './graph_primitives.mjs';

function createEdgeState(resolverWarnings) {
  return {
    edges: [],
    configNodes: new Map(),
    graphNodes: new Map(),
    expressMounts: new Map(),
    expressAppReachableRouters: new Set(),
    expressExportAliases: new Map(),
    // symbol identity -> { module, name, kind, suffix } across all files, so
    // barrel re-exports resolve through the checker's alias chain straight to
    // the declaring binding.
    expressReceiverSymbolMeta: new Map(),
    expressRouterKeys: new Set(),
    expressHandlerIdentityNodes: new Map(),
    externalPackages: new Set(),
    externalSymbols: new Set(),
    warnings: [...resolverWarnings],
    warningKeys: new Set(),
  };
}

const WILDCARD_ROUTE_METHODS = new Set(['ANY', 'ALL']);
const MAX_EXPRESS_MOUNT_PREFIXES = 256;
const MAX_EXPRESS_MOUNT_TRAVERSALS = 4096;
const MAX_CALL_EXPRESSION_CHARS = 512;

const EXPRESS_ROUTE_METHODS = new Set([
  'get',
  'post',
  'put',
  'patch',
  'delete',
  'options',
  'head',
  'all',
]);

function localTarget(declarations, moduleName, name) {
  return declarations.localSymbols.get(moduleName)?.get(name) || null;
}

function resolved(target, confidence = 'heuristic', strategy = null) {
  if (!target) return null;
  return { target, confidence, strategy };
}

function targetId(value) {
  return typeof value === 'string' ? value : value?.target || null;
}

function edgeResolution(strategy) {
  return strategy ? { status: 'resolved', strategy, fallbacks: [] } : null;
}

function compilerDeclarationName(ts, node) {
  if (ts.isConstructorDeclaration(node)) return '__init__';
  if (node.name && ts.isIdentifier(node.name)) return node.name.text;
  if (
    (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node)) &&
    !node.name
  ) {
    return 'default';
  }
  return null;
}

function compilerLocationKeys(ctx, sourceFile, node) {
  const normalized = normalizePath(sourceFile.fileName);
  const start = node.getStart(sourceFile);
  const end = node.getEnd();
  const keys = [
    `${normalized}:${node.pos}:${node.end}`,
    `${normalized}:${start}:${end}`,
  ];
  const name = compilerDeclarationName(ctx.ts, node);
  if (name) {
    const loc = lineInfo(sourceFile, node);
    keys.push(
      declarationLineKey(normalized, loc.start_line, loc.end_line, name)
    );
  }
  return keys;
}

function declarationTargetFromCompilerNode(ctx, node) {
  if (!node || !ctx.compiler?.checker) return null;
  const sourceFile = node.getSourceFile();
  for (const key of compilerLocationKeys(ctx, sourceFile, node)) {
    const target = ctx.declarations.declarationLocations.get(key);
    if (target) return target;
  }
  return null;
}

function declarationTargetFromSymbol(ctx, symbol) {
  if (!symbol || !ctx.compiler?.checker) return null;
  const checker = ctx.compiler.checker;
  const candidates = [symbol];
  if (ctx.ts.SymbolFlags && (symbol.flags & ctx.ts.SymbolFlags.Alias)) {
    try {
      candidates.push(checker.getAliasedSymbol(symbol));
    } catch {
      // Alias resolution can throw on invalid symbols; keep the extractor best effort.
    }
  }
  for (const candidate of candidates) {
    const declarations = [
      candidate.valueDeclaration,
      ...(candidate.declarations || []),
    ].filter(Boolean);
    for (const declaration of declarations) {
      const target = declarationTargetFromCompilerNode(ctx, declaration);
      if (target) return target;
    }
  }
  return null;
}

function externalSymbol(state, packageName, name) {
  const id = `extsym:${packageName}.${name}`;
  state.externalSymbols.add(id);
  state.externalPackages.add(packageName);
  return id;
}

function resolveImportedTarget(state, declarations, imported, localName, targetModule) {
  if (targetModule.kind === 'module') {
    return (
      localTarget(declarations, targetModule.file.module, imported || localName) ||
      `mod:${targetModule.file.module}`
    );
  }
  if (targetModule.kind === 'external') {
    return externalSymbol(state, targetModule.package, imported || localName);
  }
  return null;
}

function calleeText(ts, node) {
  if (ts.isIdentifier(node)) return node.text;
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  return null;
}

function expressionRootText(ts, node) {
  let current = node;
  while (ts.isPropertyAccessExpression(current)) current = current.expression;
  return ts.isIdentifier(current) ? current.text : null;
}

function expressionFullText(sourceFile, node) {
  return boundedExpressionText(sourceFile, node).value;
}

function boundedExpressionText(sourceFile, node) {
  const normalized = node.getText(sourceFile).replace(/\s+/g, '');
  return {
    value: truncateText(normalized, MAX_CALL_EXPRESSION_CHARS),
    truncated: normalized.length > MAX_CALL_EXPRESSION_CHARS,
  };
}

function stringArg(ts, node) {
  const first = node.arguments?.[0];
  if (first && (ts.isStringLiteral(first) || ts.isNoSubstitutionTemplateLiteral(first))) {
    return first.text;
  }
  return null;
}

function staticRequireSpecifier(ts, node) {
  if (
    ts.isCallExpression(node) &&
    ts.isIdentifier(node.expression) &&
    node.expression.text === 'require'
  ) {
    return stringArg(ts, node);
  }
  return null;
}

function importEqualsSpecifier(ts, node) {
  if (
    !ts.isImportEqualsDeclaration(node) ||
    !ts.isExternalModuleReference(node.moduleReference)
  ) {
    return null;
  }
  return stringLiteralText(ts, node.moduleReference.expression);
}

function stringLiteralText(ts, node) {
  if (!node) return null;
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    return node.text;
  }
  return null;
}

// Receivers are recorded per lexical scope, not per file: an unrelated object
// bound to the same name in a sibling scope must not be treated as a router.
function enclosingScope(ts, node) {
  let current = node.parent;
  while (current && !ts.isSourceFile(current) && !ts.isBlock(current) && !ts.isModuleBlock(current)) {
    current = current.parent;
  }
  return current || null;
}

function compilerSymbolIdentity(symbol) {
  const declarations = symbol?.declarations ||
    (symbol?.valueDeclaration ? [symbol.valueDeclaration] : []);
  if (declarations.length === 0) return null;
  return declarations
    .map((declaration) => {
      const sourceFile = declaration.getSourceFile?.();
      const fileName = sourceFile?.fileName
        ? normalizePath(sourceFile.fileName)
        : '';
      return `${fileName}:${declaration.pos}:${declaration.end}:${declaration.kind}`;
    })
    .sort()
    .join('|');
}

function expressReceiverKind(ctx, node, receiverNode) {
  const checker = ctx.compiler?.checker;
  if (checker) {
    try {
      const symbol = checker.getSymbolAtLocation(receiverNode);
      // A resolved non-Express symbol is authoritative too: that is how an
      // inner binding shadows an outer app or router with the same name.
      if (symbol) {
        const symbolKey = compilerSymbolIdentity(symbol);
        return symbolKey
          ? ctx.expressReceiverSymbols.get(symbolKey) || null
          : null;
      }
    } catch {
      // Invalid programs can make symbol lookup fail. Keep the lexical fallback
      // so route extraction remains best effort.
    }
  }
  const name = receiverNode.text;
  const scopes = ctx.expressReceivers.get(name);
  if (!scopes) return null;
  for (let current = node; current; current = current.parent) {
    const kind = scopes.get(current);
    if (kind) return kind;
  }
  return null;
}

function expressReceiverScopes(ts, sourceFile, checker) {
  const factories = new Set();
  const namespaces = new Set();
  const routerFactories = new Set();
  // name -> Map<scope node, 'app' | 'router'>
  const receivers = new Map();
  const receiverSymbols = new Map();
  // symbol identity -> { name, kind, suffix } for binding-accurate keys.
  const receiverSymbolMeta = new Map();

  for (const statement of sourceFile.statements) {
    if (
      importEqualsSpecifier(ts, statement) === 'express' &&
      !statement.isTypeOnly
    ) {
      factories.add(statement.name.text);
    }
    if (
      ts.isImportDeclaration(statement) &&
      ts.isStringLiteral(statement.moduleSpecifier) &&
      statement.moduleSpecifier.text === 'express'
    ) {
      const clause = statement.importClause;
      if (clause?.name) factories.add(clause.name.text);
      const bindings = clause?.namedBindings;
      if (bindings && ts.isNamespaceImport(bindings)) {
        namespaces.add(bindings.name.text);
      } else if (bindings && ts.isNamedImports(bindings)) {
        for (const element of bindings.elements) {
          const imported = element.propertyName?.text || element.name.text;
          if (imported === 'Router') routerFactories.add(element.name.text);
          if (imported === 'default') factories.add(element.name.text);
        }
      }
    }
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!declaration.initializer) continue;
      const specifier = staticRequireSpecifier(ts, declaration.initializer);
      if (specifier !== 'express') continue;
      if (ts.isIdentifier(declaration.name)) {
        factories.add(declaration.name.text);
      } else if (ts.isObjectBindingPattern(declaration.name)) {
        for (const element of declaration.name.elements) {
          if (!ts.isIdentifier(element.name)) continue;
          const imported = element.propertyName && ts.isIdentifier(element.propertyName)
            ? element.propertyName.text
            : element.name.text;
          if (imported === 'Router') routerFactories.add(element.name.text);
        }
      }
    }
  }

  // Nothing in this file imports or requires express: the walk below can only
  // ever come up empty, so skip it entirely.
  if (factories.size === 0 && routerFactories.size === 0 && namespaces.size === 0) {
    return { receivers, receiverSymbols, receiverSymbolMeta };
  }

  function expressFactoryKind(node) {
    // `new express.Router()` is valid, historically documented Express usage
    // and creates the same receiver as the call form.
    if (!ts.isCallExpression(node) && !ts.isNewExpression(node)) return null;
    if (ts.isIdentifier(node.expression)) {
      if (routerFactories.has(node.expression.text)) return 'router';
      return factories.has(node.expression.text) || namespaces.has(node.expression.text)
        ? 'app'
        : null;
    }
    if (!ts.isPropertyAccessExpression(node.expression)) return null;
    if (node.expression.name.text !== 'Router') return null;
    if (!ts.isIdentifier(node.expression.expression)) return null;
    return factories.has(node.expression.expression.text) ||
      namespaces.has(node.expression.expression.text)
      ? 'router'
      : null;
  }

  walkPreOrder(ts, sourceFile, (node) => {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer
    ) {
      const kind = expressFactoryKind(node.initializer);
      const scope = kind ? enclosingScope(ts, node) : null;
      if (scope) {
        if (!receivers.has(node.name.text)) receivers.set(node.name.text, new Map());
        receivers.get(node.name.text).set(scope, kind);
        if (checker) {
          try {
            const symbol = checker.getSymbolAtLocation(node.name);
            const symbolKey = compilerSymbolIdentity(symbol);
            if (symbolKey) {
              receiverSymbols.set(symbolKey, kind);
              // Module-level bindings keep the plain key that cross-file
              // import/export resolution uses; every other binding gets a
              // symbol-identity suffix so same-named routers in different
              // binding scopes cannot exchange mounts. The nearest Block is
              // not the binding scope: `var` in a block is module-level
              // (hoisted) while `let` in a for-head is not, so module-ness
              // is decided by whether the module's own locals hold this
              // exact symbol.
              // Directly exported declarations (`export const r = ...`)
              // live in the module symbol's exports table, not in locals.
              const moduleLocal = Boolean(
                (sourceFile.locals &&
                  typeof sourceFile.locals.get === 'function' &&
                  sourceFile.locals.get(node.name.text) === symbol) ||
                  (sourceFile.symbol &&
                    sourceFile.symbol.exports &&
                    typeof sourceFile.symbol.exports.get === 'function' &&
                    sourceFile.symbol.exports.get(node.name.text) === symbol)
              );
              receiverSymbolMeta.set(symbolKey, {
                name: node.name.text,
                kind,
                suffix: moduleLocal ? '' : `@sym:${symbolKey}`,
              });
            }
          } catch {
            // Keep the lexical fallback for malformed programs.
          }
        }
      }
    }
  });
  return { receivers, receiverSymbols, receiverSymbolMeta };
}

function expressRouteRegistration(ctx, node) {
  const ts = ctx.ts;
  if (!ts.isPropertyAccessExpression(node.expression)) return null;
  if (!ts.isIdentifier(node.expression.expression)) return null;
  const receiverNode = node.expression.expression;
  const receiver = receiverNode.text;
  const receiverKind = expressReceiverKind(ctx, node, receiverNode);
  if (!receiverKind) return null;
  const declaredMethod = node.expression.name.text.toLowerCase();
  if (!EXPRESS_ROUTE_METHODS.has(declaredMethod)) return null;
  const args = node.arguments || [];
  // `app.get(name)` with a single argument is Express's settings getter, not a
  // route: a registration always carries at least one handler.
  if (declaredMethod === 'get' && args.length === 1) return null;
  let routePath = stringArg(ts, node);
  // The empty static path is the Express idiom for matching the bare mount
  // root; it is a static string, not a dynamic path.
  if (routePath === '') routePath = '/';
  if (routePath === null || !routePath.startsWith('/')) {
    warnOnce(
      ctx.state,
      'typescript_express_route_dynamic',
      ctx.file,
      `Dynamic Express route registered through ${receiver}.${declaredMethod} cannot be resolved statically.`
    );
    return null;
  }
  return {
    receiver,
    receiverKind,
    // Resolved at the registration site so same-named routers in different
    // scopes keep distinct mount identities.
    routerKey: scopedExpressRouterKey(ctx, node, receiverNode),
    declaredMethod,
    method: declaredMethod === 'all' ? 'ALL' : declaredMethod.toUpperCase(),
    path: routePath,
    handlers: [...args].slice(1),
  };
}

// A router is identified by the module that declares it plus its local name, so
// a mount recorded in one file can be matched to registrations in another.
function expressRouterKey(module, name) {
  return `${module}::${name}`;
}

// Distinct routers may share a local name in different binding scopes of one
// file; symbol-identity metadata recorded at the declaration keys them apart
// while module-level bindings keep the plain key that cross-file import and
// export resolution relies on. The lexical nearest-scope walk remains the
// fallback when symbol resolution is unavailable.
function scopedExpressRouterKey(ctx, useNode, receiverNode) {
  const ts = ctx.ts;
  const name = receiverNode.text;
  const checker = ctx.compiler?.checker;
  if (checker) {
    try {
      const symbolKey = compilerSymbolIdentity(
        checker.getSymbolAtLocation(receiverNode)
      );
      const meta = symbolKey
        ? ctx.expressReceiverSymbolMeta.get(symbolKey)
        : null;
      if (meta) {
        return `${expressRouterKey(ctx.file.module, meta.name)}${meta.suffix}`;
      }
    } catch {
      // Invalid programs can make symbol lookup fail; use the lexical walk.
    }
  }
  let scope = null;
  const scopes = ctx.expressReceivers.get(name);
  if (scopes) {
    for (let current = useNode; current; current = current.parent) {
      if (scopes.has(current)) {
        scope = current;
        break;
      }
    }
  }
  const key = expressRouterKey(ctx.file.module, name);
  if (!scope || ts.isSourceFile(scope)) return key;
  return `${key}@${scope.pos}`;
}

function expressOriginRouterKey(state, origin) {
  // Registrations key routers by their LOCAL name in the declaring module;
  // an import that arrived through an export alias (`export default r`,
  // `export { r as apiRouter }`) must resolve back to that local name or the
  // mount silently misses the registrations.
  const aliased = state.expressExportAliases.get(
    `${origin.module}::${origin.name}`
  );
  return expressRouterKey(origin.module, aliased || origin.name);
}

function recordImportOrigin(ctx, identifier, origin) {
  ctx.importOrigins.set(identifier.text, origin);
  const checker = ctx.compiler?.checker;
  if (!checker) return;
  try {
    const symbol = checker.getSymbolAtLocation(identifier);
    const symbolKey = compilerSymbolIdentity(symbol);
    if (symbolKey) ctx.importOriginSymbols.set(symbolKey, origin);
  } catch {
    // Invalid programs can make symbol lookup fail. The textual origin remains
    // available to the best-effort fallback below.
  }
}

function expressMountedRouterKey(ctx, node) {
  if (!ctx.ts.isIdentifier(node)) return null;
  const checker = ctx.compiler?.checker;
  if (checker) {
    try {
      const symbol = checker.getSymbolAtLocation(node);
      if (symbol) {
        const symbolKey = compilerSymbolIdentity(symbol);
        const receiverKind = symbolKey
          ? ctx.expressReceiverSymbols.get(symbolKey)
          : null;
        if (receiverKind === 'router') {
          const key = scopedExpressRouterKey(ctx, node, node);
          return ctx.state.expressRouterKeys.has(key) ? key : null;
        }
        // Imports that arrive through barrels (`export { r } from ...`,
        // `export * from ...`) resolve through the checker's alias chain
        // straight to the declaring binding, at any forwarding depth.
        if ((symbol.flags & ctx.ts.SymbolFlags.Alias) !== 0) {
          try {
            const aliasedKey = compilerSymbolIdentity(
              checker.getAliasedSymbol(symbol)
            );
            const meta = aliasedKey
              ? ctx.state.expressReceiverSymbolMeta.get(aliasedKey)
              : null;
            if (meta && meta.kind === 'router') {
              const key = `${expressRouterKey(meta.module, meta.name)}${meta.suffix}`;
              return ctx.state.expressRouterKeys.has(key) ? key : null;
            }
          } catch {
            // Unresolvable alias chains fall through to the origin lookup.
          }
        }
        const origin = symbolKey
          ? ctx.importOriginSymbols.get(symbolKey)
          : null;
        if (!origin) return null;
        const key = expressOriginRouterKey(ctx.state, origin);
        return ctx.state.expressRouterKeys.has(key) ? key : null;
      }
    } catch {
      // Invalid programs can make symbol lookup fail. Keep extraction best
      // effort, but still require router evidence before recording a mount.
    }
  }

  if (expressReceiverKind(ctx, node, node) === 'router') {
    const key = scopedExpressRouterKey(ctx, node, node);
    return ctx.state.expressRouterKeys.has(key) ? key : null;
  }
  const origin = ctx.importOrigins.get(node.text);
  if (!origin) return null;
  const key = expressOriginRouterKey(ctx.state, origin);
  return ctx.state.expressRouterKeys.has(key) ? key : null;
}

// `export default router` and `export = router` — record the local name so a
// default/CommonJS import resolves to the same key the registrations use.
function collectExpressExportAliases(ctx) {
  const ts = ctx.ts;
  const setAlias = (exportedName, localName) => {
    ctx.state.expressExportAliases.set(
      `${ctx.file.module}::${exportedName}`,
      localName
    );
  };
  for (const statement of ctx.sourceFile.statements) {
    if (ts.isExportAssignment(statement)) {
      if (ts.isIdentifier(statement.expression)) {
        setAlias('default', statement.expression.text);
      }
      continue;
    }
    // `export { r as apiRouter }` and `export { r as default }` bind the
    // exported name to a different local name; without the alias record an
    // importer's mount key misses the registrations.
    if (
      ts.isExportDeclaration(statement) &&
      !statement.moduleSpecifier &&
      statement.exportClause &&
      ts.isNamedExports(statement.exportClause)
    ) {
      for (const element of statement.exportClause.elements) {
        if (element.propertyName) {
          setAlias(element.name.text, element.propertyName.text);
        }
      }
      continue;
    }
    // `module.exports = router` is the CommonJS spelling of the same default
    // export. A bare `exports = router` only rebinds the local alias and does
    // not change what Node exports.
    if (!ts.isExpressionStatement(statement)) continue;
    const expression = statement.expression;
    if (!ts.isBinaryExpression(expression)) continue;
    if (expression.operatorToken.kind !== ts.SyntaxKind.EqualsToken) continue;
    if (!ts.isIdentifier(expression.right)) continue;
    const left = expression.left;
    const isModuleExports =
      ts.isPropertyAccessExpression(left) &&
      left.name.text === 'exports' &&
      ts.isIdentifier(left.expression) &&
      left.expression.text === 'module';
    if (isModuleExports) {
      setAlias('default', expression.right.text);
    }
  }
}

function collectExpressMount(ctx, node) {
  const ts = ctx.ts;
  if (!ts.isCallExpression(node)) return;
  if (!ts.isPropertyAccessExpression(node.expression)) return;
  if (node.expression.name.text !== 'use') return;
  if (!ts.isIdentifier(node.expression.expression)) return;
  const receiverNode = node.expression.expression;
  const receiver = receiverNode.text;
  const parentKind = expressReceiverKind(ctx, node, receiverNode);
  if (!parentKind) return;
  const args = node.arguments || [];
  if (args.length < 1) return;
  // Express accepts `use([path], ...middleware)`: arity does not prove that the
  // first argument is a path. A static string is a prefix; a syntactically or
  // checker-proven callable is middleware and therefore implies the root, and
  // so is a proven registered router. An unresolved identifier can still be a
  // dynamic path, so keep that case fail closed instead of inventing a `/`
  // mount.
  const staticPrefix = stringLiteralText(ts, args[0]);
  let prefix = '/';
  let mountArguments = args;
  if (staticPrefix !== null) {
    // Express treats an empty static path as a root mount (verified against
    // Express 5.2.1), so record '/' rather than losing the mount at the
    // slash guard below.
    prefix = staticPrefix === '' ? '/' : staticPrefix;
    mountArguments = args.slice(1);
  } else if (args.length >= 2) {
    if (
      !isStaticallyCallableMiddleware(ctx, args[0]) &&
      !expressMountedRouterKey(ctx, args[0])
    ) {
      return;
    }
    mountArguments = args;
  }
  if (!prefix.startsWith('/')) return;
  for (const arg of mountArguments) {
    const key = expressMountedRouterKey(ctx, arg);
    if (!key) continue;
    const mount = {
      prefix,
      parentKind,
      parent: scopedExpressRouterKey(ctx, node, receiverNode),
    };
    const mounts = ctx.state.expressMounts.get(key) || [];
    if (
      mounts.some(
        (candidate) =>
          candidate.prefix === mount.prefix &&
          candidate.parentKind === mount.parentKind &&
          candidate.parent === mount.parent
      )
    ) {
      continue;
    }
    mounts.push(mount);
    ctx.state.expressMounts.set(key, mounts);
  }
}

// Judges by call shape, not receiver type. Express and every express-like
// router register routes as `verb(path, ...handlers)` -- a callable after
// the path -- while HTTP clients pass configs and bodies. Proving the
// receiver's Express provenance through TypeScript's type syntax was retired
// here: it required re-implementing type inference form by form (aliases,
// heritage, unions, constraints, overloads, casts, ...) and stayed one syntax
// form behind every review round. The shape rule trades that open enumeration
// for one bounded loss: callback-style clients (`request.get(url, cb)`) are
// suppressed as registration-shaped, a heuristic edge for a legacy pattern.
// Express's app.METHOD contract accepts a handler, a handler array, or a
// spread of handlers; all of them mark the registration shape.
function isStaticallyCallableHandlerArgument(ctx, argument) {
  if (isStaticallyCallableMiddleware(ctx, argument)) return true;
  const ts = ctx.ts;
  if (ts.isSpreadElement(argument)) {
    return isStaticallyCallableHandlerArgument(ctx, argument.expression);
  }
  if (ts.isArrayLiteralExpression(argument)) {
    // Express handler arrays hold handlers only: one callable member does
    // not make `[transform, { id: 1 }]` a handler array, so every member
    // must qualify.
    return (
      argument.elements.length > 0 &&
      argument.elements.every((element) =>
        isStaticallyCallableHandlerArgument(ctx, element)
      )
    );
  }
  // A value typed as an array or tuple of callables (RequestHandler[],
  // `[() => void, () => void]`) is a handler bundle even without a literal.
  const checker = ctx.compiler?.checker;
  if (!checker) return false;
  try {
    let argumentType = checker.getTypeAtLocation(argument);
    // A generic bound like `T extends Handler[]` proves every instantiation:
    // expand the base constraint before the shape checks.
    if (typeof checker.getBaseConstraintOfType === 'function') {
      const constraint = checker.getBaseConstraintOfType(argumentType);
      if (constraint) argumentType = constraint;
    }
    const elementTypes = [];
    if (
      typeof checker.isTupleType === 'function' &&
      typeof checker.getTypeArguments === 'function' &&
      checker.isTupleType(argumentType)
    ) {
      elementTypes.push(...checker.getTypeArguments(argumentType));
    } else if (
      typeof checker.getElementTypeOfArrayType === 'function' &&
      checker.getElementTypeOfArrayType(argumentType)
    ) {
      elementTypes.push(checker.getElementTypeOfArrayType(argumentType));
    } else if (typeof checker.getIndexTypeOfType === 'function') {
      // Readonly arrays and other array-likes expose their element type
      // through the numeric index signature.
      const indexed = checker.getIndexTypeOfType(
        argumentType,
        ctx.ts.IndexKind.Number
      );
      if (indexed) elementTypes.push(indexed);
    }
    return (
      elementTypes.length > 0 &&
      elementTypes.every(
        (elementType) => elementType.getCallSignatures().length > 0
      )
    );
  } catch {
    // Invalid programs can make checker queries fail; use graph evidence.
    return false;
  }
}

// Express's app.METHOD contract accepts string, array, and regex paths; all
// of them make an unresolved registration worth disclosing.
function looksLikeStaticRoutePathArgument(ts, argument) {
  const literal = stringLiteralText(ts, argument);
  if (literal !== null) return literal.startsWith('/');
  if (ts.isArrayLiteralExpression(argument)) {
    // Express path arrays may mix strings and regexes; every member must
    // itself be a static route path.
    return (
      argument.elements.length > 0 &&
      argument.elements.every((element) =>
        looksLikeStaticRoutePathArgument(ts, element)
      )
    );
  }
  return ts.isRegularExpressionLiteral(argument);
}

// `app.route('/x').get(h).post(h2)` chains register real routes that the
// static extractor does not model yet; they must be disclosed rather than
// staying silent (and must not be misread as HTTP client calls).
function unresolvedExpressRouteChain(ctx, node) {
  const ts = ctx.ts;
  if (!ts.isPropertyAccessExpression(node.expression)) return false;
  const args = node.arguments || [];
  if (
    args.length < 1 ||
    !args.every((argument) => isStaticallyCallableHandlerArgument(ctx, argument))
  ) {
    return false;
  }
  let base = node.expression.expression;
  while (ts.isCallExpression(base)) {
    const callee = base.expression;
    if (!ts.isPropertyAccessExpression(callee)) break;
    if (callee.name.text === 'route') {
      const routeArgs = base.arguments || [];
      if (
        routeArgs.length > 0 &&
        looksLikeStaticRoutePathArgument(ts, routeArgs[0])
      ) {
        warnOnce(
          ctx.state,
          'typescript_route_registration_unresolved',
          ctx.file,
          `Chained route(...) registration is not statically indexed; its routes are missing from the graph.`
        );
      }
      return true;
    }
    base = callee.expression;
  }
  return false;
}

function unresolvedRouteRegistrationShape(ctx, node) {
  const ts = ctx.ts;
  const propertyAccess = ts.isPropertyAccessExpression(node.expression);
  const elementAccess = ts.isElementAccessExpression(node.expression);
  if (!propertyAccess && !elementAccess) return false;
  const receiverNode = node.expression.expression;
  const localExpressReceiver =
    ts.isIdentifier(receiverNode) && expressReceiverKind(ctx, node, receiverNode);
  if (elementAccess) {
    const method = stringLiteralText(ts, node.expression.argumentExpression);
    // JavaScript property keys are case-sensitive.  A static uppercase key is
    // not Express's lower-case METHOD API, while a computed key is worth a
    // disclosure only when the receiver is proven to be an Express app/router.
    if (method !== null && !EXPRESS_ROUTE_METHODS.has(method)) return false;
    if (method === null && !localExpressReceiver) return false;
  } else if (!EXPRESS_ROUTE_METHODS.has(node.expression.name.text)) {
    return false;
  }
  const args = node.arguments || [];
  // A registration carries a path plus at least one handler; Express's
  // one-argument settings getter (`app.get('env')`) and one-argument client
  // helpers keep the ordinary heuristics.
  if (args.length < 2) return false;
  if (
    !args
      .slice(1)
      .some((argument) => isStaticallyCallableHandlerArgument(ctx, argument))
  ) {
    return false;
  }
  // A receiver created in this file is fully tracked by the registration
  // pass: its static routes are indexed and its dynamic paths already carry
  // the dynamic-route warning, so this guard would be both redundant and
  // wrong for it.
  if (
    propertyAccess && localExpressReceiver
  ) {
    return false;
  }
  // Warn only for statically route-like paths; cache-style APIs with
  // non-path keys stay quiet, and suppression applies either way.
  if (
    looksLikeStaticRoutePathArgument(ts, args[0]) ||
    (elementAccess && localExpressReceiver)
  ) {
    let receiverText = 'the receiver';
    try {
      receiverText = receiverNode.getText(ctx.sourceFile);
    } catch {
      // Synthesized nodes have no source text; keep the generic label.
    }
    if (receiverText.length > 80) {
      receiverText = `${receiverText.slice(0, 77)}...`;
    }
    const detail =
      elementAccess && localExpressReceiver
        ? stringLiteralText(ts, node.expression.argumentExpression) === null
          ? 'computed element-access Express registrations are not statically indexed'
          : 'element-access Express registrations are not statically indexed'
        : 'the receiver is not an app or router created in this file';
    warnOnce(
      ctx.state,
      'typescript_route_registration_unresolved',
      ctx.file,
      `Registration-shaped call through ${receiverText} is not statically indexed: ${detail}.`
    );
  }
  return true;
}

function isStaticallyCallableMiddleware(ctx, node) {
  const ts = ctx.ts;
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
    return true;
  }
  if (ts.isIdentifier(node) && expressReceiverKind(ctx, node, node)) {
    return true;
  }
  const checker = ctx.compiler?.checker;
  if (checker) {
    try {
      if (checker.getTypeAtLocation(node).getCallSignatures().length > 0) {
        return true;
      }
    } catch {
      // Invalid programs can make checker queries fail; use graph evidence.
    }
  }
  if (!ts.isIdentifier(node)) return false;
  const target = targetId(resolveExpressionTarget(ctx, node));
  return Boolean(target && (target.startsWith('fn:') || target.startsWith('method:')));
}

function visitExpressCalls(ctx, root, handler) {
  const ts = ctx.ts;
  walkPreOrder(ts, root, (node) => {
    if (ts.isCallExpression(node)) handler(ctx, node);
  });
}

function joinRoutePath(prefix, routePath) {
  if (!prefix || prefix === '/') return routePath;
  const base = prefix.endsWith('/') ? prefix.slice(0, -1) : prefix;
  if (routePath === '/') return base || '/';
  return `${base}${routePath}`;
}

function appReachableExpressRouters(expressMounts) {
  const reachable = new Set();
  const childrenByParent = new Map();
  for (const [child, mounts] of expressMounts) {
    for (const mount of mounts) {
      if (mount.parentKind === 'app') {
        reachable.add(child);
        continue;
      }
      if (!childrenByParent.has(mount.parent)) {
        childrenByParent.set(mount.parent, new Set());
      }
      childrenByParent.get(mount.parent).add(child);
    }
  }

  // Propagate app reachability from mounted parents to their child routers.
  // This is linear in the mount graph and avoids enumerating route-prefix paths.
  const queue = [...reachable].sort();
  for (let index = 0; index < queue.length; index += 1) {
    const parent = queue[index];
    const children = [...(childrenByParent.get(parent) || [])].sort();
    for (const child of children) {
      if (reachable.has(child)) continue;
      reachable.add(child);
      queue.push(child);
    }
  }
  return reachable;
}

function compareExpressMounts(left, right, appReachableRouters) {
  // Every mount whose parent can reach an app outranks router-local chains, so
  // unresolved fan-out cannot consume the traversal budget before a nested
  // reachable path is visited. Remaining ties stay deterministic.
  const leftApp =
    left.parentKind === 'app' || appReachableRouters.has(left.parent) ? 0 : 1;
  const rightApp =
    right.parentKind === 'app' || appReachableRouters.has(right.parent) ? 0 : 1;
  if (leftApp !== rightApp) return leftApp - rightApp;
  if (left.prefix !== right.prefix) {
    return left.prefix < right.prefix ? -1 : 1;
  }
  if (left.parent !== right.parent) {
    return left.parent < right.parent ? -1 : 1;
  }
  return 0;
}

// Walk every mount chain outwards. A router may intentionally be mounted at
// more than one prefix; collapsing those mounts would silently drop entrypoints.
function resolveMountPrefixes(ctx, registration) {
  if (registration.receiverKind === 'app') return [{ prefix: '', resolved: true }];
  const initialKey = registration.routerKey;
  const results = new Map();
  let traversals = 0;
  let capped = false;

  function addResult(prefix, resolved) {
    const key = `${resolved ? '1' : '0'}\u0000${prefix}`;
    if (results.has(key)) return;
    if (results.size < MAX_EXPRESS_MOUNT_PREFIXES) {
      results.set(key, { prefix, resolved });
      return;
    }
    capped = true;
    // At capacity, keep every resolved prefix we can by evicting an unresolved
    // slot. Pure unresolved overflow stays fail-closed at the bound.
    if (!resolved) return;
    let victimKey = null;
    let victimPrefix = null;
    for (const [existingKey, existing] of results) {
      if (existing.resolved) continue;
      if (victimPrefix === null || existing.prefix > victimPrefix) {
        victimKey = existingKey;
        victimPrefix = existing.prefix;
      }
    }
    if (victimKey === null) return;
    results.delete(victimKey);
    results.set(key, { prefix, resolved });
  }

  function visit(key, segments, seen) {
    traversals += 1;
    if (traversals > MAX_EXPRESS_MOUNT_TRAVERSALS) {
      capped = true;
      return;
    }
    if (seen.has(key)) {
      addResult(segments.join(''), false);
      return;
    }
    const mounts = [...(ctx.state.expressMounts.get(key) || [])].sort((left, right) =>
      compareExpressMounts(left, right, ctx.state.expressAppReachableRouters)
    );
    if (mounts.length === 0) {
      addResult(segments.join(''), false);
      return;
    }
    const nextSeen = new Set(seen);
    nextSeen.add(key);
    for (const mount of mounts) {
      if (traversals > MAX_EXPRESS_MOUNT_TRAVERSALS) {
        capped = true;
        break;
      }
      // Express ignores a trailing slash on a mount path when matching
      // (app.use('/api/', r) serves /api/v1/...), so strip it before
      // composing or the published route path gains a double slash that the
      // final joinRoutePath pass cannot repair.
      let mountPrefix = mount.prefix === '/' ? '' : mount.prefix;
      if (mountPrefix.endsWith('/')) mountPrefix = mountPrefix.slice(0, -1);
      const nextSegments = [mountPrefix, ...segments];
      if (mount.parentKind === 'app') {
        addResult(nextSegments.join(''), true);
      } else {
        visit(mount.parent, nextSegments, nextSeen);
      }
    }
  }

  visit(initialKey, [], new Set());
  if (capped) {
    warnOnce(
      ctx.state,
      'typescript_express_mount_paths_capped',
      ctx.file,
      `Express route ${registration.method} ${registration.path} exceeds the static mount expansion limit; retained at most ${MAX_EXPRESS_MOUNT_PREFIXES} deterministic prefix(es).`
    );
  }
  return [...results.values()];
}

function stableLexicalName(ctx, node) {
  if (!node) return null;
  const ts = ctx.ts;
  let text;
  if (
    ts.isIdentifier(node) ||
    (typeof ts.isPrivateIdentifier === 'function' && ts.isPrivateIdentifier(node)) ||
    ts.isStringLiteral(node) ||
    ts.isNumericLiteral(node) ||
    ts.isNoSubstitutionTemplateLiteral(node)
  ) {
    text = String(node.text);
  } else {
    text = node.getText(ctx.sourceFile).trim();
  }
  if (!text) return null;
  if (/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(text)) return text;
  return `sha256_${crypto.createHash('sha256').update(text).digest('hex').slice(0, 16)}`;
}

function anonymousFunctionDescriptor(ctx, node) {
  const ts = ctx.ts;
  const parent = node.parent;
  if (parent && ts.isCallExpression(parent)) {
    const argumentIndex = parent.arguments.findIndex(
      (candidate) => candidate === node
    );
    if (argumentIndex >= 0) {
      const calleeHash = crypto
        .createHash('sha256')
        .update(expressionFullText(ctx.sourceFile, parent.expression))
        .digest('hex')
        .slice(0, 16);
      return `callback:${calleeHash}:argument:${argumentIndex}`;
    }
    if (parent.expression === node) return 'immediate-callback';
  }
  return 'anonymous-function';
}

function lexicalScopeDescriptor(ctx, node) {
  const ts = ctx.ts;
  if (ts.isMethodDeclaration(node)) {
    return `method:${stableLexicalName(ctx, node.name) || 'anonymous'}`;
  }
  if (ts.isGetAccessorDeclaration(node)) {
    return `getter:${stableLexicalName(ctx, node.name) || 'anonymous'}`;
  }
  if (ts.isSetAccessorDeclaration(node)) {
    return `setter:${stableLexicalName(ctx, node.name) || 'anonymous'}`;
  }
  if (ts.isConstructorDeclaration(node)) return 'constructor';
  if (ts.isFunctionDeclaration(node)) {
    return `function:${stableLexicalName(ctx, node.name) || 'default'}`;
  }
  if (ts.isFunctionExpression(node)) {
    const name = stableLexicalName(ctx, node.name);
    return name ? `function:${name}` : anonymousFunctionDescriptor(ctx, node);
  }
  if (ts.isArrowFunction(node)) {
    return anonymousFunctionDescriptor(ctx, node);
  }
  if (ts.isVariableDeclaration(node)) {
    return `binding:${stableLexicalName(ctx, node.name) || 'anonymous'}`;
  }
  if (ts.isPropertyAssignment(node) || ts.isPropertyDeclaration(node)) {
    return `property:${stableLexicalName(ctx, node.name) || 'anonymous'}`;
  }
  if (
    ts.isBinaryExpression(node) &&
    node.operatorToken.kind === ts.SyntaxKind.EqualsToken
  ) {
    return `assignment:${stableLexicalName(ctx, node.left) || 'anonymous'}`;
  }
  return null;
}

function expressHandlerEnclosingSubject(ctx, handler) {
  const descriptors = [];
  for (
    let current = handler.parent;
    current && !ctx.ts.isSourceFile(current);
    current = current.parent
  ) {
    const target = declarationTargetFromCompilerNode(ctx, current);
    if (target) {
      return [target, ...descriptors.reverse()].join('::');
    }
    const descriptor = lexicalScopeDescriptor(ctx, current);
    if (descriptor) descriptors.push(descriptor);
  }
  return [ctx.moduleId, ...descriptors.reverse()].join('::');
}

function expressHandlerNode(
  ctx,
  handler,
  registration,
  routePath,
  index,
  { shared = false } = {}
) {
  const loc = lineInfo(ctx.sourceFile, handler);
  const routeSlug = routePath
    .replace(/[^A-Za-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '') || 'root';
  const routeHash = crypto
    .createHash('sha256')
    .update(routePath)
    .digest('hex')
    .slice(0, 8);
  const bodyHash = crypto
    .createHash('sha256')
    .update(handler.getText(ctx.sourceFile).trim())
    .digest('hex');
  const enclosingSubject = expressHandlerEnclosingSubject(ctx, handler);
  const enclosingSubjectHash = crypto
    .createHash('sha256')
    .update(enclosingSubject)
    .digest('hex')
    .slice(0, 16);
  // A callback registered through a const bundle can serve several routes,
  // so its identity must be route-neutral: derived from the declaration
  // alone, it survives inserting, removing, or reordering the registrations
  // that share it. Directly inlined callbacks are unique AST nodes and keep
  // the route-bound identity.
  const identityKey = shared
    ? [ctx.file.module, enclosingSubject, 'shared', bodyHash].join('\u0000')
    : [
        ctx.file.module,
        enclosingSubject,
        registration.receiver,
        registration.method,
        routePath,
        index,
        bodyHash,
      ].join('\u0000');
  const identityNodes =
    ctx.state.expressHandlerIdentityNodes.get(identityKey) || [];
  const identityOccurrence = identityNodes.length + 1;
  const qualname = shared
    ? [
        ctx.file.module,
        `__express_shared_scope_${enclosingSubjectHash}_handler_${bodyHash.slice(0, 12)}_${identityOccurrence}`,
      ].join('.')
    : [
        ctx.file.module,
        `__express_${registration.receiver}_${registration.method.toLowerCase()}_${routeSlug}_${routeHash}_scope_${enclosingSubjectHash}_handler_${index}_${bodyHash.slice(0, 12)}_${identityOccurrence}`,
      ].join('.');
  const handlerNode = {
    id: `fn:${qualname}`,
    kind: 'function',
    name: shared
      ? 'shared Express handler'
      : `${registration.method} ${routePath} handler`,
    qualname,
    path: ctx.file.path,
    start_line: loc.start_line,
    end_line: loc.end_line,
    properties: {
      language: 'typescript',
      frontend_name: 'typescript-static',
      frontend_version: '0.1.0',
      framework: 'express',
      route_handler: true,
      synthetic: true,
      identity_profile: shared
        ? 'typescript_express_shared_handler_v1'
        : 'typescript_express_inline_handler_v2',
      declaring_module: ctx.file.module,
      handler_enclosing_subject: enclosingSubject,
      // Route bindings are omitted for shared handlers: any value would be
      // an accident of which registration ran first.
      ...(shared
        ? { shared_route_handler: true }
        : {
            route_receiver: registration.receiver,
            route_method: registration.method,
            route_path: routePath,
            route_handler_index: index,
          }),
      handler_body_sha256: bodyHash,
      stable_handler_occurrence: identityOccurrence,
      stable_handler_ambiguous: false,
    },
  };
  identityNodes.push(handlerNode);
  if (identityNodes.length > 1) {
    for (const candidate of identityNodes) {
      candidate.properties.stable_handler_ambiguous = true;
    }
  }
  ctx.state.expressHandlerIdentityNodes.set(identityKey, identityNodes);
  return handlerNode;
}

// Parentheses, `as`, `satisfies`, angle-bracket assertions, and non-null `!`
// are transparent at runtime; every handler-position check must see through
// all of them, not a hand-picked subset.
function unwrapTransparentExpression(ts, expression) {
  if (typeof ts.skipOuterExpressions === 'function') {
    return ts.skipOuterExpressions(expression);
  }
  let current = expression;
  for (;;) {
    if (ts.isParenthesizedExpression(current)) current = current.expression;
    else if (ts.isAsExpression(current)) current = current.expression;
    else if (
      typeof ts.isSatisfiesExpression === 'function' &&
      ts.isSatisfiesExpression(current)
    ) {
      current = current.expression;
    } else if (ts.isNonNullExpression(current)) current = current.expression;
    else if (ts.isTypeAssertionExpression(current)) current = current.expression;
    else return current;
  }
}

function outermostTransparentExpression(ts, expression) {
  let current = expression;
  for (;;) {
    const parent = current.parent;
    if (!parent || parent.expression !== current) return current;
    if (ts.isParenthesizedExpression(parent)) current = parent;
    else if (ts.isAsExpression(parent)) current = parent;
    else if (
      typeof ts.isSatisfiesExpression === 'function' &&
      ts.isSatisfiesExpression(parent)
    ) {
      current = parent;
    } else if (ts.isNonNullExpression(parent)) current = parent;
    else if (ts.isTypeAssertionExpression(parent)) current = parent;
    else return current;
  }
}

// A const binding enumerates as a handler bundle only when its initializer is
// an array literal AND the binding provably still holds that literal at
// registration time: not exported, and referenced nowhere except its own
// declaration and route-registration argument positions. A callable
// initializer is NOT followed -- the identifier itself resolves to the
// declared function, and copying it into a synthetic node would fork its
// identity.
function enumerableConstArrayInitializer(ctx, expression) {
  const ts = ctx.ts;
  if (!ts.isIdentifier(expression)) return null;
  const checker = ctx.compiler?.checker;
  if (!checker) return null;
  let symbol = null;
  try {
    symbol = checker.getSymbolAtLocation(expression);
  } catch {
    return null;
  }
  const declaration = symbol?.valueDeclaration;
  if (
    !declaration ||
    !ts.isVariableDeclaration(declaration) ||
    !declaration.initializer ||
    !ts.isVariableDeclarationList(declaration.parent) ||
    (declaration.parent.flags & ts.NodeFlags.Const) === 0
  ) {
    return null;
  }
  const initializer = unwrapTransparentExpression(ts, declaration.initializer);
  if (!ts.isArrayLiteralExpression(initializer)) return null;
  const name = expression.text;
  try {
    if (ctx.sourceFile.symbol?.exports?.get?.(name) === symbol) return null;
  } catch {
    return null;
  }
  let safe = true;
  walkPreOrder(ts, ctx.sourceFile, (candidate) => {
    if (!safe) return;
    if (!ts.isIdentifier(candidate) || candidate.text !== name) return;
    if (candidate === declaration.name) return;
    let candidateSymbol = null;
    try {
      candidateSymbol = checker.getSymbolAtLocation(candidate);
    } catch {
      safe = false;
      return;
    }
    if (candidateSymbol !== symbol) return;
    // Allowed occurrence: a direct argument (or spread operand) of a
    // route-registration verb call. Anything else -- a mutating method
    // receiver, an element write, or being passed elsewhere -- may change
    // the array before registration.
    let argument = outermostTransparentExpression(ts, candidate);
    if (
      ts.isSpreadElement(argument.parent) &&
      argument.parent.expression === argument
    ) {
      argument = argument.parent;
    }
    const call = argument.parent;
    // A verb-shaped name alone is no proof: `sink.get(..., bundle)` on an
    // arbitrary object may mutate the bundle. The occurrence is safe only
    // when the call's receiver is a tracked Express app or router.
    const isRegistrationArgument =
      call &&
      ts.isCallExpression(call) &&
      call.arguments.includes(argument) &&
      ts.isPropertyAccessExpression(call.expression) &&
      EXPRESS_ROUTE_METHODS.has(call.expression.name.text.toLowerCase()) &&
      ts.isIdentifier(call.expression.expression) &&
      Boolean(expressReceiverKind(ctx, call, call.expression.expression));
    if (!isRegistrationArgument) safe = false;
  });
  return safe ? initializer : null;
}

function collectExpressRoute(ctx, node) {
  const registration = expressRouteRegistration(ctx, node);
  if (!registration) return;
  ctx.expressRouteRegistrations.add(node);
  const loc = lineInfo(ctx.sourceFile, node);
  // Express accepts arrays (and spreads) of handlers; expand every
  // statically enumerable collection -- through parentheses, casts, and
  // const array initializers -- so its members get the same treatment as
  // bare handlers, and disclose any handler bundle that cannot be
  // enumerated instead of dropping it silently.
  const handlerExpressions = [];
  let handlerListComplete = true;
  const warnUnresolvedHandlers = () => {
    handlerListComplete = false;
    warnOnce(
      ctx.state,
      'typescript_express_handlers_unresolved',
      ctx.file,
      `Express handler list for ${registration.method} ${registration.path} contains a handler bundle that cannot be statically enumerated; those handlers are not linked.`
    );
  };
  const expandHandler = (expression, seen, viaBinding) => {
    if (seen.has(expression)) return;
    seen.add(expression);
    const ts = ctx.ts;
    const unwrapped = unwrapTransparentExpression(ts, expression);
    if (unwrapped !== expression) {
      expandHandler(unwrapped, seen, viaBinding);
      return;
    }
    if (ts.isArrayLiteralExpression(expression)) {
      for (const element of expression.elements) {
        expandHandler(element, seen, viaBinding);
      }
      return;
    }
    if (ts.isSpreadElement(expression)) {
      // A spread is a bundle by construction: either its operand unwraps to
      // enumerable members, or the bundle is disclosed -- never silent.
      const inner = unwrapTransparentExpression(ts, expression.expression);
      if (ts.isArrayLiteralExpression(inner)) {
        expandHandler(inner, seen, viaBinding);
        return;
      }
      const spreadInitializer = enumerableConstArrayInitializer(ctx, inner);
      if (spreadInitializer) {
        // Reached through a binding: any callback inside can serve several
        // registrations, so its synthetic identity must be route-neutral.
        expandHandler(spreadInitializer, seen, true);
        return;
      }
      warnUnresolvedHandlers();
      return;
    }
    const initializer = enumerableConstArrayInitializer(ctx, expression);
    if (initializer) {
      expandHandler(initializer, seen, true);
      return;
    }
    // Anything array- or tuple-typed that survived the unwrapping above is a
    // handler bundle we cannot enumerate: disclose it rather than letting it
    // fall through to single-expression resolution and a silent null target.
    if (
      !isStaticallyCallableMiddleware(ctx, expression) &&
      isStaticallyCallableHandlerArgument(ctx, expression)
    ) {
      warnUnresolvedHandlers();
      return;
    }
    handlerExpressions.push({ expression, shared: Boolean(viaBinding) });
  };
  for (const handler of registration.handlers) {
    expandHandler(handler, new Set(), false);
  }
  const registrationId = [
    ctx.file.path,
    loc.start_line,
    loc.column,
    registration.method,
    registration.path,
  ].join(':');
  const targets = handlerExpressions.map(({ expression: handler, shared }, index) => {
    if (ctx.ts.isArrowFunction(handler) || ctx.ts.isFunctionExpression(handler)) {
      // A callback shared between registrations (through a const bundle)
      // keeps ONE route-neutral synthetic identity: every route invokes the
      // same node, its internal calls are attributed once, and the identity
      // does not depend on which registration is seen first.
      const existing = ctx.routeHandlerScopes.get(handler);
      if (existing) {
        return { handler, target: existing, index };
      }
      const handlerNode = expressHandlerNode(
        ctx,
        handler,
        registration,
        registration.path,
        index,
        { shared }
      );
      ctx.state.graphNodes.set(handlerNode.id, handlerNode);
      ctx.routeHandlerScopes.set(handler, handlerNode.id);
      return { handler, target: handlerNode.id, index };
    }
    return {
      handler,
      target: targetId(resolveExpressionTarget(ctx, handler)),
      index,
    };
  });

  for (const mount of resolveMountPrefixes(ctx, registration)) {
    const routePath = joinRoutePath(mount.prefix, registration.path);
    // Without a mount chain reaching an `express()` app the path is router-local,
    // not a URL. Qualify the id by module so two routers that both declare the
    // same local path stay distinct instead of collapsing into one node.
    const routeNodeId = mount.resolved
      ? `route:${registration.method}:${routePath}`
      : `route:${registration.method}:${routePath}@${ctx.file.module}`;
    if (!mount.resolved) {
      warnOnce(
        ctx.state,
        'typescript_express_route_unmounted',
        ctx.file,
        `Express router ${registration.receiver} has no statically resolvable mount point; ${registration.method} ${registration.path} is recorded router-local.`
      );
    }
    if (!ctx.state.graphNodes.has(routeNodeId)) {
      ctx.state.graphNodes.set(routeNodeId, {
        id: routeNodeId,
        kind: 'route',
        name: `${registration.method} ${routePath}`,
        qualname: routeNodeId,
        path: ctx.file.path,
        start_line: loc.start_line,
        end_line: loc.end_line,
        properties: {
          language: 'typescript',
          frontend_name: 'typescript-static',
          frontend_version: '0.1.0',
          framework: 'express',
          router: registration.receiver,
          route_kind: 'express_route',
          route_path: routePath,
          router_local_path: registration.path,
          route_mounted: mount.resolved,
          http_method: registration.method,
          declared_method: registration.declaredMethod.toUpperCase(),
        },
      });
    }

    for (const { handler, target, index } of targets) {
      if (!target) continue;
      ctx.state.edges.push(
        makeEdge(
          routeNodeId,
          target,
          'invokes',
          evidence(
            'ts_express_route',
            ctx.file,
            ctx.sourceFile,
            handler,
            `${registration.method} ${routePath}`
          ),
          'inferred',
          {
            framework: 'express',
            router: registration.receiver,
            http_method: registration.method,
            http_path: registration.path,
            resolved_http_path: routePath,
            route_handler_index: index,
            route_handler_count: handlerExpressions.length,
            route_handler_count_known: handlerListComplete,
            route_registration_id: registrationId,
            route_registration_line: loc.start_line,
            route_registration_column: loc.column,
            route_registration_complete: handlerListComplete,
            registration_role: !handlerListComplete
              ? 'unknown'
              : index === handlerExpressions.length - 1
                ? 'terminal_handler'
                : 'pre_handler_middleware',
          },
          edgeResolution('express_static_route')
        )
      );
    }
  }
}

function isProcessEnvExpression(ts, node) {
  return (
    ts.isPropertyAccessExpression(node) &&
    node.name.text === 'env' &&
    ts.isIdentifier(node.expression) &&
    node.expression.text === 'process'
  );
}

function isImportMetaEnvExpression(ts, node) {
  return (
    ts.isPropertyAccessExpression(node) &&
    node.name.text === 'env' &&
    ts.isMetaProperty(node.expression) &&
    node.expression.keywordToken === ts.SyntaxKind.ImportKeyword &&
    node.expression.name.text === 'meta'
  );
}

function envAccess(ts, node) {
  if (ts.isPropertyAccessExpression(node)) {
    const envRoot = node.expression;
    if (isProcessEnvExpression(ts, envRoot) || isImportMetaEnvExpression(ts, envRoot)) {
      const syntax = isProcessEnvExpression(ts, envRoot) ? 'process.env' : 'import.meta.env';
      return {
        key: node.name.text,
        dynamic: false,
        syntax,
      };
    }
  }
  if (
    ts.isElementAccessExpression(node) &&
    (isProcessEnvExpression(ts, node.expression) || isImportMetaEnvExpression(ts, node.expression))
  ) {
    const syntax = isProcessEnvExpression(ts, node.expression) ? 'process.env[]' : 'import.meta.env[]';
    const key = stringLiteralText(ts, node.argumentExpression);
    return { key, dynamic: key === null, syntax };
  }
  return null;
}

function storageReceiverName(ts, node) {
  if (ts.isIdentifier(node)) {
    return node.text === 'localStorage' || node.text === 'sessionStorage' ? node.text : null;
  }
  if (
    ts.isPropertyAccessExpression(node) &&
    (node.name.text === 'localStorage' || node.name.text === 'sessionStorage') &&
    ts.isIdentifier(node.expression) &&
    (node.expression.text === 'window' || node.expression.text === 'globalThis')
  ) {
    return node.name.text;
  }
  return null;
}

function storageCall(ts, node) {
  if (!ts.isPropertyAccessExpression(node.expression)) return null;
  const storage = storageReceiverName(ts, node.expression.expression);
  if (!storage) return null;
  const method = node.expression.name.text;
  if (!['getItem', 'setItem', 'removeItem'].includes(method)) return null;
  const key = stringArg(ts, node);
  return {
    storage,
    method,
    key,
    dynamic: key === null,
    edgeKind: method === 'getItem' ? 'reads' : 'writes',
  };
}

function importWarningSource(reason) {
  return reason.startsWith('workspace_package_')
    ? 'workspace package exports'
    : reason.startsWith('package_')
      ? 'package exports/imports'
      : reason.startsWith('bundler_alias')
        ? 'bundler alias'
      : 'tsconfig paths';
}

function addConfigNode(state, id, name, file, sourceFile, node, properties = {}) {
  if (!state.configNodes.has(id)) {
    state.configNodes.set(id, makeConfigNode(id, name, file, sourceFile, node, properties));
  }
  return id;
}

function warnOnce(state, kind, file, message) {
  const key = `${kind}:${file.path}:${message}`;
  if (state.warningKeys.has(key)) return;
  state.warningKeys.add(key);
  state.warnings.push({ kind, path: file.path, message });
}

function stripApiPrefix(apiPath) {
  return apiPath.replace(/^\/api(?:\/v\d[^/]*)?(?=\/|$)/, '') || '/';
}

function matchingRouteTier(methodRoutes, rawPath) {
  if (rawPath.startsWith('/api/')) {
    return {
      route: methodRoutes.find((route) => route.path === rawPath) || null,
      ambiguous: [],
    };
  }
  const suffixMatches = methodRoutes.filter((route) => stripApiPrefix(route.path) === rawPath);
  if (suffixMatches.length === 1) {
    return { route: suffixMatches[0], ambiguous: [] };
  }
  return { route: null, ambiguous: suffixMatches };
}

function matchingRoute(routeMatches, method, rawPath) {
  if (!rawPath || !rawPath.startsWith('/')) return { route: null, ambiguous: [] };
  const upper = method.toUpperCase();
  const isWildcardRequest = WILDCARD_ROUTE_METHODS.has(upper);
  // Wildcards are a fallback, not a peer: folding them in with the exact-method
  // routes would turn a unique match into a spurious ambiguity.
  const tiers = [routeMatches.filter((route) => route.method === upper)];
  if (!isWildcardRequest) {
    tiers.push(routeMatches.filter((route) => WILDCARD_ROUTE_METHODS.has(route.method)));
  }
  for (const tier of tiers) {
    if (tier.length === 0) continue;
    const match = matchingRouteTier(tier, rawPath);
    if (match.route || match.ambiguous.length > 0) return match;
  }
  return { route: null, ambiguous: [] };
}

function resolveName(ctx, name) {
  return localTarget(ctx.declarations, ctx.file.module, name) || ctx.importBindings.get(name) || null;
}

function resolveModuleMember(ctx, moduleId, memberName) {
  if (!moduleId?.startsWith('mod:')) return null;
  return localTarget(ctx.declarations, moduleId.slice(4), memberName);
}

function currentScopeForNode(ctx, node, classStack) {
  const ts = ctx.ts;
  const routeHandlerScope = ctx.routeHandlerScopes.get(node);
  if (routeHandlerScope) return routeHandlerScope;
  if (ts.isFunctionDeclaration(node) && node.name) {
    return resolveName(ctx, node.name.text);
  }
  if (
    ts.isVariableDeclaration(node) &&
    ts.isIdentifier(node.name) &&
    node.initializer &&
    (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))
  ) {
    return resolveName(ctx, node.name.text);
  }
  if ((ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node)) && classStack.length) {
    const methodName = ts.isConstructorDeclaration(node) ? '__init__' : node.name?.text;
    if (!methodName) return null;
    return `method:${ctx.file.module}.${classStack[classStack.length - 1]}.${methodName}`;
  }
  return null;
}

function resolveExpressionTarget(ctx, expr) {
  const ts = ctx.ts;
  if (ts.isIdentifier(expr)) {
    const lexical = resolveName(ctx, expr.text);
    if (
      lexical &&
      !lexical.startsWith('unresolved:') &&
      !lexical.startsWith('mod:')
    ) {
      return resolved(lexical);
    }
    return resolveTypeCheckerExpressionTarget(ctx, expr) || resolved(lexical);
  }
  if (ts.isPropertyAccessExpression(expr)) {
    const root = expressionRootText(ts, expr);
    const rootTarget = root ? resolveName(ctx, root) : null;
    if (rootTarget?.startsWith('mod:')) {
      const memberTarget = resolveModuleMember(ctx, rootTarget, expr.name.text);
      if (memberTarget) return resolved(memberTarget);
    }
    if (rootTarget?.startsWith('class:')) {
      return resolved(`method:${rootTarget.slice(6)}.${expr.name.text}`);
    }
    if (rootTarget?.startsWith('ext:')) {
      return resolved(externalSymbol(ctx.state, rootTarget.slice(4), expr.name.text));
    }
    const typed = resolveTypeCheckerExpressionTarget(ctx, expr);
    if (typed) return typed;
  }
  return null;
}

function resolveTypeCheckerExpressionTarget(ctx, expr) {
  const checker = ctx.compiler?.checker;
  if (!checker) return null;
  const ts = ctx.ts;
  const symbolLocations = [];
  if (ts.isPropertyAccessExpression(expr)) {
    symbolLocations.push(expr.name, expr);
  } else {
    symbolLocations.push(expr);
  }
  for (const location of symbolLocations) {
    const symbol = checker.getSymbolAtLocation(location);
    const target = declarationTargetFromSymbol(ctx, symbol);
    if (target) {
      return resolved(target, 'inferred', 'typescript_typechecker');
    }
  }
  return null;
}

function collectImports(ctx) {
  const ts = ctx.ts;
  for (const stmt of ctx.sourceFile.statements) {
    if (!ts.isImportDeclaration(stmt) || !stmt.moduleSpecifier || !ts.isStringLiteral(stmt.moduleSpecifier)) {
      continue;
    }
    const specifier = stmt.moduleSpecifier.text;
    const targetModule = ctx.resolveImport(specifier, ctx.file);
    if (targetModule.kind === 'unresolved') {
      const reason = targetModule.reason || '';
      if (reason.endsWith('_ambiguous')) {
        const source = importWarningSource(reason);
        warnOnce(
          ctx.state,
          'typescript_import_ambiguous',
          ctx.file,
          `Ambiguous TypeScript import ${specifier} from ${source}; candidates: ${(targetModule.candidates || []).join(', ')}`
        );
      } else if (reason.endsWith('_unresolved')) {
        const source = importWarningSource(reason);
        warnOnce(
          ctx.state,
          'typescript_import_unresolved',
          ctx.file,
          `TypeScript import ${specifier} matched ${source} but no indexed file.`
        );
      }
    }
    const clause = stmt.importClause;
    const namedBindings = clause?.namedBindings;
    const importKind =
      clause?.isTypeOnly ||
      (
        !clause?.name &&
        namedBindings &&
        ts.isNamedImports(namedBindings) &&
        namedBindings.elements.length > 0 &&
        namedBindings.elements.every((element) => element.isTypeOnly)
      )
        ? 'type'
        : 'value';
    if (targetModule.kind === 'external') ctx.state.externalPackages.add(targetModule.package);
    const targetId =
      targetModule.kind === 'module'
        ? `mod:${targetModule.file.module}`
        : targetModule.kind === 'external'
          ? `ext:${targetModule.package}`
          : `unresolved:${specifier}`;
    ctx.state.edges.push(
      makeEdge(
        ctx.moduleId,
        targetId,
        'imports',
        evidence('ts_import', ctx.file, ctx.sourceFile, stmt, specifier),
        targetModule.kind === 'unresolved' ? 'unresolved' : 'confirmed',
        { specifier, import_kind: importKind }
      )
    );

    const originModule = targetModule.kind === 'module' ? targetModule.file.module : null;
    if (!clause) continue;
    if (clause.name) {
      ctx.importBindings.set(
        clause.name.text,
        resolveImportedTarget(ctx.state, ctx.declarations, 'default', clause.name.text, targetModule)
      );
      if (originModule) {
        recordImportOrigin(ctx, clause.name, {
          module: originModule,
          name: 'default',
        });
      }
    }
    const bindings = clause.namedBindings;
    if (!bindings) continue;
    if (ts.isNamespaceImport(bindings)) {
      ctx.importBindings.set(bindings.name.text, targetId);
    } else if (ts.isNamedImports(bindings)) {
      for (const element of bindings.elements) {
        const imported = element.propertyName?.text || element.name.text;
        ctx.importBindings.set(
          element.name.text,
          resolveImportedTarget(ctx.state, ctx.declarations, imported, element.name.text, targetModule)
        );
        if (originModule) {
          recordImportOrigin(ctx, element.name, {
            module: originModule,
            name: imported,
          });
        }
      }
    }
  }
}

function addRequireImportEdge(
  ctx,
  node,
  specifier,
  targetModule,
  importKind = 'commonjs'
) {
  if (targetModule.kind === 'unresolved') {
    warnOnce(
      ctx.state,
      'typescript_import_unresolved',
      ctx.file,
      `TypeScript require ${specifier} could not be resolved statically.`
    );
  }
  if (targetModule.kind === 'external') ctx.state.externalPackages.add(targetModule.package);
  const targetId =
    targetModule.kind === 'module'
      ? `mod:${targetModule.file.module}`
      : targetModule.kind === 'external'
        ? `ext:${targetModule.package}`
        : `unresolved:${specifier}`;
  ctx.state.edges.push(
    makeEdge(
      ctx.moduleId,
      targetId,
      'imports',
      evidence('ts_commonjs_require', ctx.file, ctx.sourceFile, node, specifier),
      targetModule.kind === 'unresolved' ? 'unresolved' : 'confirmed',
      { specifier, import_kind: importKind }
    )
  );
  return targetId;
}

function collectCommonJsImports(ctx) {
  const ts = ctx.ts;
  for (const stmt of ctx.sourceFile.statements) {
    const importEquals = importEqualsSpecifier(ts, stmt);
    if (importEquals) {
      const targetModule = ctx.resolveImport(importEquals, ctx.file);
      const targetId = addRequireImportEdge(
        ctx,
        stmt,
        importEquals,
        targetModule,
        stmt.isTypeOnly ? 'type' : 'commonjs'
      );
      ctx.importBindings.set(stmt.name.text, targetId);
      if (targetModule.kind === 'module') {
        recordImportOrigin(ctx, stmt.name, {
          module: targetModule.file.module,
          name: 'default',
        });
      }
      continue;
    }
    if (!ts.isVariableStatement(stmt)) continue;
    for (const declaration of stmt.declarationList.declarations) {
      const specifier = declaration.initializer
        ? staticRequireSpecifier(ts, declaration.initializer)
        : null;
      if (!specifier) continue;
      const targetModule = ctx.resolveImport(specifier, ctx.file);
      const targetId = addRequireImportEdge(ctx, declaration, specifier, targetModule);
      const originModule =
        targetModule.kind === 'module' ? targetModule.file.module : null;
      if (ts.isIdentifier(declaration.name)) {
        ctx.importBindings.set(declaration.name.text, targetId);
        if (originModule) {
          recordImportOrigin(ctx, declaration.name, {
            module: originModule,
            name: 'default',
          });
        }
      } else if (ts.isObjectBindingPattern(declaration.name)) {
        for (const element of declaration.name.elements) {
          if (!ts.isIdentifier(element.name)) continue;
          const imported = element.propertyName && ts.isIdentifier(element.propertyName)
            ? element.propertyName.text
            : element.name.text;
          if (originModule) {
            recordImportOrigin(ctx, element.name, {
              module: originModule,
              name: imported,
            });
          }
          ctx.importBindings.set(
            element.name.text,
            resolveImportedTarget(ctx.state, ctx.declarations, imported, element.name.text, targetModule)
          );
        }
      }
    }
  }
}

function dynamicImportSpecifier(ctx, node) {
  const ts = ctx.ts;
  if (node.expression.kind !== ts.SyntaxKind.ImportKeyword) return null;
  const first = node.arguments?.[0];
  const specifier = stringLiteralText(ts, first);
  return {
    specifier,
    dynamic: specifier === null,
  };
}

function collectDynamicImportEdges(ctx, node, currentScope) {
  const dynamicImport = dynamicImportSpecifier(ctx, node);
  if (!dynamicImport) return false;
  if (dynamicImport.dynamic) {
    warnOnce(
      ctx.state,
      'typescript_dynamic_import_unresolved',
      ctx.file,
      'Dynamic TypeScript import expression cannot be resolved statically.'
    );
    return true;
  }

  const specifier = dynamicImport.specifier;
  const targetModule = ctx.resolveImport(specifier, ctx.file);
  if (targetModule.kind === 'unresolved') {
    const reason = targetModule.reason || '';
    if (reason.endsWith('_ambiguous')) {
      warnOnce(
        ctx.state,
        'typescript_import_ambiguous',
        ctx.file,
        `Ambiguous TypeScript import ${specifier} from ${importWarningSource(reason)}; candidates: ${(targetModule.candidates || []).join(', ')}`
      );
    } else if (reason.endsWith('_unresolved')) {
      warnOnce(
        ctx.state,
        'typescript_import_unresolved',
        ctx.file,
        `TypeScript import ${specifier} matched ${importWarningSource(reason)} but no indexed file.`
      );
    }
  }
  if (targetModule.kind === 'external') ctx.state.externalPackages.add(targetModule.package);
  const targetId =
    targetModule.kind === 'module'
      ? `mod:${targetModule.file.module}`
      : targetModule.kind === 'external'
        ? `ext:${targetModule.package}`
        : `unresolved:${specifier}`;
  ctx.state.edges.push(
    makeEdge(
      currentScope,
      targetId,
      'imports',
      evidence('ts_dynamic_import', ctx.file, ctx.sourceFile, node, specifier),
      targetModule.kind === 'unresolved' ? 'unresolved' : 'heuristic',
      { specifier, import_kind: 'dynamic' }
    )
  );
  return true;
}

function collectDefinitionEdges(ctx) {
  for (const graphNode of ctx.declarations.nodes.filter((candidate) => candidate.path === ctx.file.path)) {
    ctx.state.edges.push(
      makeEdge(
        ctx.moduleId,
        graphNode.id,
        'defines',
        {
          kind: 'ts_define',
          path: ctx.file.path,
          start_line: graphNode.start_line,
          end_line: graphNode.end_line,
          detail: graphNode.name,
        },
        'confirmed'
      )
    );
    if (ctx.declarations.exportedSymbols.has(graphNode.id)) {
      ctx.state.edges.push(
        makeEdge(
          ctx.moduleId,
          graphNode.id,
          'exports',
          {
            kind: 'ts_export',
            path: ctx.file.path,
            start_line: graphNode.start_line,
            end_line: graphNode.end_line,
            detail: graphNode.name,
          },
          'confirmed'
        )
      );
    }
  }
}

function callResultUsage(ts, node) {
  let current = node;
  let parent = current.parent;
  let awaited = false;
  while (parent) {
    if (ts.isParenthesizedExpression(parent) && parent.expression === current) {
      current = parent;
      parent = current.parent;
      continue;
    }
    if (
      (ts.isAsExpression(parent) ||
        ts.isTypeAssertionExpression(parent) ||
        ts.isNonNullExpression(parent) ||
        ts.isSatisfiesExpression?.(parent)) &&
      parent.expression === current
    ) {
      current = parent;
      parent = current.parent;
      continue;
    }
    if (ts.isAwaitExpression(parent) && parent.expression === current) {
      awaited = true;
      current = parent;
      parent = current.parent;
      continue;
    }
    break;
  }
  if (!parent) return { awaited, usage: 'unknown', used: null };
  if (ts.isExpressionStatement(parent)) {
    return { awaited, usage: 'discarded', used: false };
  }
  if (ts.isVoidExpression(parent)) {
    return { awaited, usage: 'discarded', used: false };
  }
  if (ts.isReturnStatement(parent)) {
    return { awaited, usage: 'returned', used: true };
  }
  if (ts.isArrowFunction(parent) && parent.body === current) {
    return { awaited, usage: 'returned', used: true };
  }
  if (ts.isVariableDeclaration(parent) && parent.initializer === current) {
    return { awaited, usage: 'assigned', used: true };
  }
  if (
    ts.isBinaryExpression(parent) &&
    parent.right === current &&
    parent.operatorToken.kind >= ts.SyntaxKind.FirstAssignment &&
    parent.operatorToken.kind <= ts.SyntaxKind.LastAssignment
  ) {
    return { awaited, usage: 'assigned', used: true };
  }
  if (
    (ts.isCallExpression(parent) || ts.isNewExpression(parent)) &&
    [...(parent.arguments || [])].includes(current)
  ) {
    return { awaited, usage: 'passed_as_argument', used: true };
  }
  if (
    ts.isIfStatement(parent) ||
    ts.isWhileStatement(parent) ||
    ts.isDoStatement(parent) ||
    (ts.isConditionalExpression(parent) && parent.condition === current)
  ) {
    return { awaited, usage: 'condition', used: true };
  }
  return { awaited, usage: 'used', used: true };
}

function callsiteFact(ctx, node) {
  const ts = ctx.ts;
  const args = [...(node.arguments || [])];
  const hasSpread = args.some((argument) => ts.isSpreadElement(argument));
  const loc = lineInfo(ctx.sourceFile, node);
  const result = callResultUsage(ts, node);
  const rawExpression = boundedExpressionText(ctx.sourceFile, node);
  const calleeExpression = boundedExpressionText(ctx.sourceFile, node.expression);
  return {
    path: ctx.file.path,
    line: loc.start_line,
    column: loc.column,
    raw_expression: rawExpression.value,
    raw_expression_truncated: rawExpression.truncated,
    callee_expression: calleeExpression.value,
    callee_expression_truncated: calleeExpression.truncated,
    argument_count: hasSpread ? null : args.length,
    syntactic_argument_count: args.length,
    argument_count_known: !hasSpread,
    has_spread_argument: hasSpread,
    awaited: result.awaited,
    return_value_usage: result.usage,
    return_value_used: result.used,
  };
}

function callsiteProperties(ctx, node) {
  const fact = callsiteFact(ctx, node);
  return { callsite: fact, callsites: [fact] };
}

function collectCallEdges(ctx, node, currentScope) {
  const ts = ctx.ts;
  const name = calleeText(ts, node.expression);
  const resolvedCall = resolveExpressionTarget(ctx, node.expression);
  const target = targetId(resolvedCall);
  if (target) {
    ctx.state.edges.push(
      makeEdge(
        currentScope,
        target,
        'calls',
        evidence('ts_call', ctx.file, ctx.sourceFile, node, expressionFullText(ctx.sourceFile, node.expression)),
        resolvedCall.confidence || 'heuristic',
        callsiteProperties(ctx, node),
        edgeResolution(resolvedCall.strategy)
      )
    );
    if (name && isHookName(name)) {
      ctx.state.edges.push(
        makeEdge(
          currentScope,
          target,
          'uses_hook',
          evidence('ts_hook', ctx.file, ctx.sourceFile, node, name),
          resolvedCall.confidence || 'heuristic',
          callsiteProperties(ctx, node),
          edgeResolution(resolvedCall.strategy)
        )
      );
    }
  }

  if (name && isHookName(name) && !target) {
    const root = expressionRootText(ts, node.expression);
    const packageName = root && ctx.importBindings.get(root)?.startsWith('ext:')
      ? ctx.importBindings.get(root).slice(4)
      : 'react';
    ctx.state.edges.push(
      makeEdge(
        currentScope,
        externalSymbol(ctx.state, packageName, name),
        'uses_hook',
        evidence('ts_hook', ctx.file, ctx.sourceFile, node, name),
        'heuristic',
        callsiteProperties(ctx, node)
      )
    );
  }

  // Outbound client-call matching is intentionally property-access-only.
  // Element access is registration disclosure territory and must not turn an
  // arbitrary `obj["get"]("/path")` into a fabricated route call edge.
  const methodName = name?.toUpperCase();
  const httpMethods = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']);
  // The receiver guard covers every Express route method -- head, options,
  // and all included -- so an unindexable registration always surfaces a
  // warning; the outbound HTTP client heuristic below stays limited to the
  // five verbs clients use.
  // The chain check runs first: a terminal chain like
  // `app.route('/users').get(auth, listUsers)` also satisfies the generic
  // shape (trailing callable), and short-circuiting there would skip the
  // chain's disclosure warning.
  const unresolvedRegistration =
    !ctx.expressRouteRegistrations.has(node) &&
    ((Boolean(name) &&
      EXPRESS_ROUTE_METHODS.has(name) &&
      unresolvedExpressRouteChain(ctx, node)) ||
      unresolvedRouteRegistrationShape(ctx, node));
  if (
    !unresolvedRegistration &&
    methodName &&
    httpMethods.has(methodName) &&
    !ctx.expressRouteRegistrations.has(node)
  ) {
    const rawHttpPath = stringArg(ts, node);
    const { route, ambiguous } = matchingRoute(ctx.routeMatches, methodName, rawHttpPath);
    if (route) {
      ctx.state.edges.push(
        makeEdge(
          currentScope,
          route.id,
          'calls',
          evidence('ts_http_call', ctx.file, ctx.sourceFile, node, `${methodName} ${rawHttpPath}`),
          'heuristic',
          { http_method: methodName, http_path: rawHttpPath }
        )
      );
    } else if (ambiguous.length) {
      const candidateIds = ambiguous.map((candidate) => candidate.id).sort();
      ctx.state.warnings.push({
        kind: 'typescript_route_ambiguous',
        path: ctx.file.path,
        message: `Ambiguous TypeScript HTTP route ${methodName} ${rawHttpPath}; candidates: ${candidateIds.join(', ')}`,
      });
    }
  }

  collectFetchRouteEdge(ctx, node, currentScope);
}

function fetchCall(ts, node) {
  const expression = node.expression;
  let isFetch = ts.isIdentifier(expression) && expression.text === 'fetch';
  if (
    !isFetch &&
    ts.isPropertyAccessExpression(expression) &&
    expression.name.text === 'fetch'
  ) {
    isFetch =
      (ts.isIdentifier(expression.expression) &&
        ['window', 'globalThis'].includes(expression.expression.text));
  }
  if (!isFetch) return null;

  const rawHttpPath = stringArg(ts, node);
  const first = node.arguments?.[0];
  const dynamicPath =
    first !== undefined &&
    !(ts.isStringLiteral(first) || ts.isNoSubstitutionTemplateLiteral(first));
  const dynamicRouteCandidate =
    dynamicPath &&
    ts.isTemplateExpression(first) &&
    first.head.text.startsWith('/');
  let method = 'GET';
  const options = node.arguments?.[1];
  if (options && ts.isObjectLiteralExpression(options)) {
    for (const prop of options.properties) {
      if (!ts.isPropertyAssignment(prop)) continue;
      const name = prop.name;
      const propertyName =
        ts.isIdentifier(name) || ts.isStringLiteral(name) ? name.text : null;
      if (propertyName === 'method') {
        const literal = stringLiteralText(ts, prop.initializer);
        if (literal) method = literal.toUpperCase();
      }
    }
  }
  return { rawHttpPath, dynamicPath, dynamicRouteCandidate, method };
}

function collectFetchRouteEdge(ctx, node, currentScope) {
  const fetchInfo = fetchCall(ctx.ts, node);
  if (!fetchInfo) return;
  if (fetchInfo.dynamicPath) {
    if (!fetchInfo.dynamicRouteCandidate) return;
    warnOnce(
      ctx.state,
      'typescript_route_dynamic',
      ctx.file,
      'Dynamic TypeScript fetch URL cannot be resolved to a backend route statically.'
    );
    return;
  }
  const httpMethods = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']);
  if (!httpMethods.has(fetchInfo.method)) return;
  const { route, ambiguous } = matchingRoute(ctx.routeMatches, fetchInfo.method, fetchInfo.rawHttpPath);
  if (route) {
    ctx.state.edges.push(
      makeEdge(
        currentScope,
        route.id,
        'calls',
        evidence('ts_fetch_call', ctx.file, ctx.sourceFile, node, `${fetchInfo.method} ${fetchInfo.rawHttpPath}`),
        'heuristic',
        {
          http_method: fetchInfo.method,
          http_path: fetchInfo.rawHttpPath,
          http_client: 'fetch',
        }
      )
    );
  } else if (ambiguous.length) {
    const candidateIds = ambiguous.map((candidate) => candidate.id).sort();
    warnOnce(
      ctx.state,
      'typescript_route_ambiguous',
      ctx.file,
      `Ambiguous TypeScript fetch route ${fetchInfo.method} ${fetchInfo.rawHttpPath}; candidates: ${candidateIds.join(', ')}`
    );
  }
}

function collectStorageEdges(ctx, node, currentScope) {
  const storage = storageCall(ctx.ts, node);
  if (!storage) return;
  if (storage.dynamic) {
    warnOnce(
      ctx.state,
      'typescript_config_dynamic',
      ctx.file,
      `Dynamic TypeScript browser storage key ${storage.storage}.${storage.method} cannot be resolved statically.`
    );
    return;
  }
  const targetId = addConfigNode(
    ctx.state,
    `config:browser_storage:${storage.storage}:${storage.key}`,
    `${storage.storage}:${storage.key}`,
    ctx.file,
    ctx.sourceFile,
    node,
    {
      config_kind: 'browser_storage',
      storage: storage.storage,
      key: storage.key,
    }
  );
  ctx.state.edges.push(
    makeEdge(
      currentScope,
      targetId,
      storage.edgeKind,
      evidence('ts_browser_storage', ctx.file, ctx.sourceFile, node, `${storage.storage}.${storage.method}(${storage.key})`),
      'heuristic',
      {
        resource_kind: 'browser_storage',
        storage: storage.storage,
        key: storage.key,
        method: storage.method,
      }
    )
  );
}

function collectEnvEdges(ctx, node, currentScope) {
  const env = envAccess(ctx.ts, node);
  if (!env) return;
  if (env.dynamic) {
    warnOnce(
      ctx.state,
      'typescript_config_dynamic',
      ctx.file,
      `Dynamic TypeScript environment key ${env.syntax} cannot be resolved statically.`
    );
    return;
  }
  const targetId = addConfigNode(
    ctx.state,
    `config:env:${env.key}`,
    env.key,
    ctx.file,
    ctx.sourceFile,
    node,
    {
      config_kind: 'env',
      key: env.key,
      syntax: env.syntax,
    }
  );
  ctx.state.edges.push(
    makeEdge(
      currentScope,
      targetId,
      'configures',
      evidence('ts_env_access', ctx.file, ctx.sourceFile, node, `${env.syntax}.${env.key}`),
      'heuristic',
      {
        resource_kind: 'env',
        env_key: env.key,
        syntax: env.syntax,
      }
    )
  );
}

function collectJsxRenderEdges(ctx, node, currentScope) {
  const ts = ctx.ts;
  if (!ts.isJsxOpeningElement(node) && !ts.isJsxSelfClosingElement(node)) return;
  const tag = node.tagName;
  const tagName = tag.getText(ctx.sourceFile).split('.')[0];
  if (isComponentName(tagName)) {
    const target = resolveName(ctx, tagName);
    if (target) {
      ctx.state.edges.push(
        makeEdge(
          currentScope,
          target,
          'renders',
          evidence('ts_jsx_render', ctx.file, ctx.sourceFile, node, tagName),
          'heuristic'
        )
      );
    }
  }
}

function visitGraphNode(ctx, root, scopeStack, classStack) {
  walkPreOrder(
    ctx.ts,
    root,
    (node, context) => {
      const [nextScope, nextClass] = collectGraphNode(
        ctx,
        node,
        context.scopeStack,
        context.classStack
      );
      return { scopeStack: nextScope, classStack: nextClass };
    },
    { scopeStack, classStack }
  );
}

// One node's share of the graph walk. Returns the scope and class stacks the
// node's children inherit.
function collectGraphNode(ctx, node, scopeStack, classStack) {
  const ts = ctx.ts;
  let nextScope = scopeStack;
  let nextClass = classStack;
  if (ts.isClassDeclaration(node) && node.name) {
    nextClass = [...classStack, node.name.text];
    const classId = resolveName(ctx, node.name.text);
    if (classId && node.heritageClauses) {
      for (const clause of node.heritageClauses) {
        for (const type of clause.types) {
          const targetName = calleeText(ts, type.expression);
          const target = targetName ? resolveName(ctx, targetName) : null;
          if (!target) continue;
          ctx.state.edges.push(
            makeEdge(
              classId,
              target,
              clause.token === ts.SyntaxKind.ImplementsKeyword ? 'implements' : 'extends',
              evidence('ts_heritage', ctx.file, ctx.sourceFile, type, targetName),
              'confirmed'
            )
          );
        }
      }
    }
  }

  const scoped = currentScopeForNode(ctx, node, nextClass);
  if (scoped) nextScope = [...scopeStack, scoped];
  const currentScope = nextScope[nextScope.length - 1] || ctx.moduleId;

  if (ts.isInterfaceDeclaration(node) && node.heritageClauses) {
    const source = resolveName(ctx, node.name.text);
    for (const clause of node.heritageClauses) {
      for (const type of clause.types) {
        const targetName = calleeText(ts, type.expression);
        const target = targetName ? resolveName(ctx, targetName) : null;
        if (source && target) {
          ctx.state.edges.push(
            makeEdge(
              source,
              target,
              'extends',
              evidence('ts_heritage', ctx.file, ctx.sourceFile, type, targetName),
              'confirmed'
            )
          );
        }
      }
    }
  }

  if (ts.isExportDeclaration(node)) {
    collectExportEdges(ctx, node);
  }

  if (ts.isExportAssignment(node)) {
    collectExportAssignmentEdge(ctx, node);
  }

  if (ts.isBinaryExpression(node)) {
    collectCommonJsExportEdges(ctx, node);
  }

  if (ts.isCallExpression(node)) {
    if (!collectDynamicImportEdges(ctx, node, currentScope)) {
      collectCallEdges(ctx, node, currentScope);
    }
    collectStorageEdges(ctx, node, currentScope);
  }

  collectEnvEdges(ctx, node, currentScope);
  collectJsxRenderEdges(ctx, node, currentScope);

  return [nextScope, nextClass];
}

function collectExportEdges(ctx, node) {
  const ts = ctx.ts;
  if (node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
    const targetModule = ctx.resolveImport(node.moduleSpecifier.text, ctx.file);
    const targetId = targetModule.kind === 'module' ? `mod:${targetModule.file.module}` : null;
    if (targetId) {
      ctx.state.edges.push(
        makeEdge(
          ctx.moduleId,
          targetId,
          'exports',
          evidence('ts_export', ctx.file, ctx.sourceFile, node, node.moduleSpecifier.text),
          'confirmed',
          { export_kind: node.isTypeOnly ? 'type' : 'value' }
        )
      );
    }
  }
  if (node.exportClause && ts.isNamedExports(node.exportClause)) {
    for (const element of node.exportClause.elements) {
      const name = element.propertyName?.text || element.name.text;
      const target = resolveName(ctx, name);
      if (target) {
        ctx.state.edges.push(
          makeEdge(
            ctx.moduleId,
            target,
            'exports',
            evidence('ts_export', ctx.file, ctx.sourceFile, element, name),
            'confirmed',
            { export_kind: node.isTypeOnly || element.isTypeOnly ? 'type' : 'value' }
          )
        );
      }
    }
  }
}

function collectExportAssignmentEdge(ctx, node) {
  const target = targetId(resolveExpressionTarget(ctx, node.expression));
  if (!target) return;
  ctx.state.edges.push(
    makeEdge(
      ctx.moduleId,
      target,
      'exports',
      evidence(
        node.isExportEquals ? 'ts_export_equals' : 'ts_default_export',
        ctx.file,
        ctx.sourceFile,
        node,
        node.expression.getText(ctx.sourceFile)
      ),
      'confirmed',
      { export_kind: node.isExportEquals ? 'commonjs' : 'default' }
    )
  );
}

function isCommonJsExportsExpression(ts, node) {
  if (ts.isIdentifier(node) && node.text === 'exports') return true;
  return (
    ts.isPropertyAccessExpression(node) &&
    node.name.text === 'exports' &&
    ts.isIdentifier(node.expression) &&
    node.expression.text === 'module'
  );
}

function commonJsExportName(ts, node) {
  if (!ts.isPropertyAccessExpression(node)) return null;
  if (isCommonJsExportsExpression(ts, node.expression)) return node.name.text;
  return null;
}

function collectCommonJsExportEdges(ctx, node) {
  const ts = ctx.ts;
  if (node.operatorToken.kind !== ts.SyntaxKind.EqualsToken) return;
  if (isCommonJsExportsExpression(ts, node.left) && ts.isObjectLiteralExpression(node.right)) {
    for (const prop of node.right.properties) {
      let target = null;
      let name = null;
      if (ts.isShorthandPropertyAssignment(prop)) {
        name = prop.name.text;
        target = targetId(resolveExpressionTarget(ctx, prop.name));
      } else if (ts.isPropertyAssignment(prop)) {
        name = prop.name && (ts.isIdentifier(prop.name) || ts.isStringLiteral(prop.name))
          ? prop.name.text
          : null;
        target = targetId(resolveExpressionTarget(ctx, prop.initializer));
      }
      if (!target) continue;
      ctx.state.edges.push(
        makeEdge(
          ctx.moduleId,
          target,
          'exports',
          evidence('ts_commonjs_export', ctx.file, ctx.sourceFile, prop, name || 'module.exports'),
          'confirmed',
          { export_kind: 'commonjs' }
        )
      );
    }
    return;
  }

  const exportName = commonJsExportName(ts, node.left);
  if (!exportName) return;
  const target = targetId(resolveExpressionTarget(ctx, node.right));
  if (!target) return;
  ctx.state.edges.push(
    makeEdge(
      ctx.moduleId,
      target,
      'exports',
      evidence('ts_commonjs_export', ctx.file, ctx.sourceFile, node, exportName),
      'confirmed',
      { export_kind: 'commonjs' }
    )
  );
}

export function collectEdges(
  ts,
  files,
  parsed,
  declarations,
  resolveImport,
  routes,
  resolverWarnings = [],
  compiler = null
) {
  const state = createEdgeState(resolverWarnings);
  // Mutable so Express routes discovered below become visible to every file's
  // client-call resolution, exactly as pre-indexed routes already are.
  const routeMatches = [...(routes || [])];
  const contexts = [];

  for (const file of files) {
    const sourceFile =
      compiler?.sourceFilesByPath?.get(normalizePath(file.absPath || file.path)) ||
      parsed.get(file.path);
    if (!sourceFile || sourceFile.error) {
      state.warnings.push({
        kind: 'typescript_parse_error',
        message: String(sourceFile?.error || 'Unable to parse TypeScript file'),
        path: file.path,
      });
      continue;
    }
    const moduleId = `mod:${file.module}`;
    const ctx = {
      ts,
      file,
      sourceFile,
      moduleId,
      importBindings: new Map(),
      importOrigins: new Map(),
      importOriginSymbols: new Map(),
      expressReceivers: new Map(),
      expressReceiverSymbols: new Map(),
      expressRouteRegistrations: new Set(),
      routeHandlerScopes: new Map(),
      state,
      declarations,
      resolveImport,
      routeMatches,
      compiler,
    };

    collectImports(ctx);
    collectCommonJsImports(ctx);
    const expressReceiverResult = expressReceiverScopes(
      ts,
      sourceFile,
      compiler?.checker
    );
    ctx.expressReceivers = expressReceiverResult.receivers;
    ctx.expressReceiverSymbols = expressReceiverResult.receiverSymbols;
    ctx.expressReceiverSymbolMeta = expressReceiverResult.receiverSymbolMeta;
    collectExpressExportAliases(ctx);
    contexts.push(ctx);
  }

  for (const ctx of contexts) {
    // Symbol-accurate keys are authoritative; the lexical scope-position keys
    // stay registered so files whose symbol lookup failed still resolve.
    for (const [symbolKey, meta] of ctx.expressReceiverSymbolMeta.entries()) {
      state.expressReceiverSymbolMeta.set(symbolKey, {
        ...meta,
        module: ctx.file.module,
      });
      if (meta.kind !== 'router') continue;
      state.expressRouterKeys.add(
        `${expressRouterKey(ctx.file.module, meta.name)}${meta.suffix}`
      );
    }
    for (const [name, scopes] of ctx.expressReceivers.entries()) {
      for (const [scope, kind] of scopes.entries()) {
        if (kind !== 'router') continue;
        const key = expressRouterKey(ctx.file.module, name);
        state.expressRouterKeys.add(
          scope === ctx.sourceFile ? key : `${key}@${scope.pos}`
        );
      }
    }
  }

  // Express routes are resolved before the main walk in three ordered passes:
  // every mount must be known before any route id is built, and every route
  // node must exist before client calls are matched against it.
  for (const ctx of contexts) {
    if (ctx.expressReceivers.size > 0 || ctx.expressReceiverSymbols.size > 0) {
      visitExpressCalls(ctx, ctx.sourceFile, collectExpressMount);
    }
  }
  state.expressAppReachableRouters = appReachableExpressRouters(
    state.expressMounts
  );
  for (const ctx of contexts) {
    if (ctx.expressReceivers.size > 0 || ctx.expressReceiverSymbols.size > 0) {
      visitExpressCalls(ctx, ctx.sourceFile, collectExpressRoute);
    }
  }
  for (const graphNode of state.graphNodes.values()) {
    if (graphNode.kind !== 'route' || graphNode.properties.route_mounted === false) continue;
    routeMatches.push({
      id: graphNode.id,
      method: graphNode.properties.http_method,
      path: graphNode.properties.route_path,
    });
  }

  for (const ctx of contexts) {
    collectDefinitionEdges(ctx);
    visitGraphNode(ctx, ctx.sourceFile, [ctx.moduleId], []);
  }

  return {
    config_nodes: [...state.configNodes.values()],
    graph_nodes: [...state.graphNodes.values()],
    edges: state.edges,
    external_packages: [...state.externalPackages].sort(),
    external_symbols: [...state.externalSymbols].sort(),
    warnings: state.warnings,
  };
}

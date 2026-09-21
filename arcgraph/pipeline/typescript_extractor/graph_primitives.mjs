export function normalizePath(value) {
  return value.replaceAll('\\', '/');
}

export function lineInfo(sourceFile, node) {
  const pos = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
  const end = sourceFile.getLineAndCharacterOfPosition(node.getEnd());
  return {
    start_line: pos.line + 1,
    end_line: end.line + 1,
    column: pos.character + 1,
  };
}

export function declarationLineKey(path, startLine, endLine, name) {
  return `${normalizePath(path)}:line:${startLine}:${endLine}:${name}`;
}

export function evidence(kind, file, sourceFile, node, detail) {
  const loc = lineInfo(sourceFile, node);
  return {
    kind,
    path: file.path,
    start_line: loc.start_line,
    end_line: loc.end_line,
    column: loc.column,
    detail,
  };
}

export function externalPackageName(specifier) {
  if (specifier.startsWith('@')) {
    return specifier.split('/').slice(0, 2).join('/');
  }
  return specifier.split('/')[0];
}

export function hasModifier(ts, node, kind) {
  return Boolean(ts.getModifiers?.(node)?.some((modifier) => modifier.kind === kind));
}

export function hasExportModifier(ts, node) {
  return (
    hasModifier(ts, node, ts.SyntaxKind.ExportKeyword) ||
    hasModifier(ts, node, ts.SyntaxKind.DefaultKeyword)
  );
}

export function hasDefaultModifier(ts, node) {
  return hasModifier(ts, node, ts.SyntaxKind.DefaultKeyword);
}

// Overflow-safe pre-order AST walk shared by every traversal in the
// extractor: one deeply nested expression chain -- routine in bundled or
// minified files -- must never overflow the V8 stack, because a single
// extractor process serves the whole repository. The visitor receives
// (node, context); returning a value replaces the context the node's
// children inherit, and returning WALK_STOP ends the walk immediately.
// Children are pushed in reverse so the traversal stays pre-order.
export function truncateText(value, limit) {
  // slice() cuts UTF-16 code units, so an astral character straddling the
  // limit would leave a lone surrogate that no UTF-8 consumer can encode.
  // Every truncated source text in the index goes through here.
  const sliced = String(value).slice(0, limit);
  const lastUnit = sliced.charCodeAt(sliced.length - 1);
  return lastUnit >= 0xd800 && lastUnit <= 0xdbff ? sliced.slice(0, -1) : sliced;
}

export const WALK_STOP = Symbol('walk-stop');

export function walkPreOrder(ts, root, visit, initialContext) {
  const stack = [{ node: root, context: initialContext }];
  const children = [];
  while (stack.length > 0) {
    const frame = stack.pop();
    const result = visit(frame.node, frame.context);
    if (result === WALK_STOP) return;
    const childContext = result === undefined ? frame.context : result;
    children.length = 0;
    ts.forEachChild(frame.node, (child) => {
      children.push(child);
    });
    for (let index = children.length - 1; index >= 0; index -= 1) {
      stack.push({ node: children[index], context: childContext });
    }
  }
}

export function containsJsx(ts, node) {
  let found = false;
  walkPreOrder(ts, node, (current) => {
    if (
      current.kind === ts.SyntaxKind.JsxElement ||
      current.kind === ts.SyntaxKind.JsxSelfClosingElement ||
      current.kind === ts.SyntaxKind.JsxFragment
    ) {
      found = true;
      return WALK_STOP;
    }
    return undefined;
  });
  return found;
}

export function isComponentName(name) {
  return /^[A-Z]/.test(name);
}

export function isHookName(name) {
  return /^use[A-Z0-9]/.test(name);
}

export function symbolKindForFunction(ts, name, node) {
  return isComponentName(name) && containsJsx(ts, node) ? 'component' : 'function';
}

export function symbolId(kind, qualname) {
  if (kind === 'class') return `class:${qualname}`;
  if (kind === 'component') return `component:${qualname}`;
  if (kind === 'interface') return `interface:${qualname}`;
  if (kind === 'type_alias') return `type_alias:${qualname}`;
  if (kind === 'enum') return `enum:${qualname}`;
  if (kind === 'method') return `method:${qualname}`;
  return `fn:${qualname}`;
}

export function makeNode(kind, name, qualname, file, sourceFile, node, properties = {}) {
  const loc = lineInfo(sourceFile, node);
  return {
    id: symbolId(kind, qualname),
    kind,
    name,
    qualname,
    path: file.path,
    start_line: loc.start_line,
    end_line: loc.end_line,
    properties: {
      ...properties,
      language: 'typescript',
      frontend_name: 'typescript-static',
      frontend_version: '0.1.0',
    },
  };
}

export function makeEdge(
  source,
  target,
  kind,
  ev,
  confidence = 'heuristic',
  properties = {},
  resolution = null
) {
  const edge = {
    source,
    target,
    kind,
    confidence,
    evidence: [ev],
    properties: {
      ...properties,
      language: 'typescript',
      frontend_name: 'typescript-static',
      frontend_version: '0.1.0',
    },
  };
  if (resolution) edge.resolution = resolution;
  return edge;
}

export function makeConfigNode(id, name, file, sourceFile, node, properties = {}) {
  const loc = lineInfo(sourceFile, node);
  return {
    id,
    kind: 'config',
    name,
    qualname: id.replace(/^config:/, ''),
    path: file.path,
    start_line: loc.start_line,
    end_line: loc.end_line,
    properties: {
      ...properties,
      language: 'typescript',
      frontend_name: 'typescript-static',
      frontend_version: '0.1.0',
    },
  };
}

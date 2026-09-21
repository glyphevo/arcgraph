import {
  declarationLineKey,
  hasDefaultModifier,
  hasExportModifier,
  isHookName,
  lineInfo,
  makeNode,
  normalizePath,
  symbolKindForFunction,
} from './graph_primitives.mjs';

const DECLARATION_FILE_SUFFIXES = ['.d.ts', '.d.mts', '.d.cts'];

function isDeclarationFile(path) {
  return DECLARATION_FILE_SUFFIXES.some((suffix) => path.endsWith(suffix));
}

function declarationLocationKeys(file, sourceFile, node, name) {
  const normalizedAbs = normalizePath(file.absPath || file.path);
  const normalizedPath = normalizePath(file.path);
  const start = node.getStart(sourceFile);
  const end = node.getEnd();
  const loc = lineInfo(sourceFile, node);
  return [
    `${normalizedAbs}:${node.pos}:${node.end}`,
    `${normalizedAbs}:${start}:${end}`,
    `${normalizedPath}:${node.pos}:${node.end}`,
    `${normalizedPath}:${start}:${end}`,
    declarationLineKey(normalizedAbs, loc.start_line, loc.end_line, name),
    declarationLineKey(normalizedPath, loc.start_line, loc.end_line, name),
  ];
}

export function collectDeclarations(ts, files, parsed) {
  const nodes = [];
  const localSymbols = new Map();
  const classNames = new Map();
  const exportedSymbols = new Set();
  const declarationLocations = new Map();
  const syntaxById = new Map();
  const modulesByDeclarationId = new Map();

  function addLocal(moduleName, name, id) {
    if (!localSymbols.has(moduleName)) localSymbols.set(moduleName, new Map());
    localSymbols.get(moduleName).set(name, id);
  }

  function addNode(node, syntaxNode = null, file = null, sourceFile = null) {
    nodes.push(node);
    if (file?.module) modulesByDeclarationId.set(node.id, file.module);
    const name = node.name;
    addLocal(node.qualname.split('.').slice(0, -1).join('.'), name, node.id);
    if (node.kind === 'class') classNames.set(node.qualname, node.name);
    if (syntaxNode && file && sourceFile) {
      syntaxById.set(node.id, { syntaxNode, file, sourceFile });
      for (const key of declarationLocationKeys(
        file,
        sourceFile,
        syntaxNode,
        node.name
      )) {
        declarationLocations.set(key, node.id);
      }
    }
  }

  for (const file of files) {
    const sourceFile = parsed.get(file.path);
    if (!sourceFile || sourceFile.error) continue;
    const moduleId = `mod:${file.module}`;
    function define(node) {
      if (ts.isFunctionDeclaration(node) && (node.name || hasDefaultModifier(ts, node))) {
        const name = node.name?.text || 'default';
        const kind = symbolKindForFunction(ts, name, node);
        const qualname = `${file.module}.${name}`;
        const graphNode = makeNode(kind, name, qualname, file, sourceFile, node, {
          exported: hasExportModifier(ts, node),
          default_export: hasDefaultModifier(ts, node),
          is_component: kind === 'component',
          is_hook: isHookName(name),
          declaration_file: isDeclarationFile(file.path),
        });
        addNode(graphNode, node, file, sourceFile);
        if (hasExportModifier(ts, node)) exportedSymbols.add(graphNode.id);
        if (hasDefaultModifier(ts, node)) addLocal(file.module, 'default', graphNode.id);
      } else if (ts.isVariableStatement(node)) {
        const exported = hasExportModifier(ts, node);
        const declarationFile = isDeclarationFile(file.path);
        for (const declaration of node.declarationList.declarations) {
          if (!ts.isIdentifier(declaration.name) || !declaration.initializer) continue;
          if (
            !ts.isArrowFunction(declaration.initializer) &&
            !ts.isFunctionExpression(declaration.initializer)
          ) {
            continue;
          }
          const name = declaration.name.text;
          const kind = symbolKindForFunction(ts, name, declaration.initializer);
          const qualname = `${file.module}.${name}`;
          const graphNode = makeNode(kind, name, qualname, file, sourceFile, declaration, {
            exported,
            is_component: kind === 'component',
            is_hook: isHookName(name),
            declaration_file: declarationFile,
          });
          addNode(graphNode, declaration, file, sourceFile);
          if (exported) exportedSymbols.add(graphNode.id);
        }
      } else if (ts.isClassDeclaration(node) && (node.name || hasDefaultModifier(ts, node))) {
        const name = node.name?.text || 'default';
        const qualname = `${file.module}.${name}`;
        const graphNode = makeNode('class', name, qualname, file, sourceFile, node, {
          exported: hasExportModifier(ts, node),
          default_export: hasDefaultModifier(ts, node),
          declaration_file: isDeclarationFile(file.path),
        });
        addNode(graphNode, node, file, sourceFile);
        if (hasExportModifier(ts, node)) exportedSymbols.add(graphNode.id);
        if (hasDefaultModifier(ts, node)) addLocal(file.module, 'default', graphNode.id);
        for (const member of node.members) {
          let methodName = null;
          if (ts.isConstructorDeclaration(member)) {
            methodName = '__init__';
          } else if (
            ts.isMethodDeclaration(member) &&
            member.name &&
            ts.isIdentifier(member.name)
          ) {
            methodName = member.name.text;
          }
          if (!methodName) continue;
          const methodQualname = `${qualname}.${methodName}`;
          const methodNode = makeNode('method', methodName, methodQualname, file, sourceFile, member, {
            class_name: name,
            declaration_file: isDeclarationFile(file.path),
          });
          addNode(methodNode, member, file, sourceFile);
        }
      } else if (ts.isInterfaceDeclaration(node)) {
        const name = node.name.text;
        const qualname = `${file.module}.${name}`;
        const graphNode = makeNode('interface', name, qualname, file, sourceFile, node, {
          exported: hasExportModifier(ts, node),
          declaration_file: isDeclarationFile(file.path),
        });
        addNode(graphNode, node, file, sourceFile);
        if (hasExportModifier(ts, node)) exportedSymbols.add(graphNode.id);
      } else if (ts.isTypeAliasDeclaration(node)) {
        const name = node.name.text;
        const qualname = `${file.module}.${name}`;
        const graphNode = makeNode('type_alias', name, qualname, file, sourceFile, node, {
          exported: hasExportModifier(ts, node),
          declaration_file: isDeclarationFile(file.path),
        });
        addNode(graphNode, node, file, sourceFile);
        if (hasExportModifier(ts, node)) exportedSymbols.add(graphNode.id);
      } else if (ts.isEnumDeclaration(node)) {
        const name = node.name.text;
        const qualname = `${file.module}.${name}`;
        const graphNode = makeNode('enum', name, qualname, file, sourceFile, node, {
          exported: hasExportModifier(ts, node),
          declaration_file: isDeclarationFile(file.path),
        });
        addNode(graphNode, node, file, sourceFile);
        if (hasExportModifier(ts, node)) exportedSymbols.add(graphNode.id);
      }
    }
    ts.forEachChild(sourceFile, define);
    for (const graphNode of nodes.filter((candidate) => candidate.path === file.path)) {
      graphNode._moduleId = moduleId;
    }
  }

  return {
    nodes,
    localSymbols,
    classNames,
    exportedSymbols,
    declarationLocations,
    syntaxById,
    modulesByDeclarationId,
  };
}

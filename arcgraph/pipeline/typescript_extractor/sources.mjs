import fs from 'node:fs';

export function parseSources(ts, files) {
  const parsed = new Map();
  for (const file of files) {
    try {
      const source = fs.readFileSync(file.absPath, 'utf8');
      const kind = file.path.endsWith('.tsx')
        ? ts.ScriptKind.TSX
        : file.path.endsWith('.jsx')
          ? ts.ScriptKind.JSX
          : file.path.endsWith('.js') ||
              file.path.endsWith('.mjs') ||
              file.path.endsWith('.cjs')
            ? ts.ScriptKind.JS
            : ts.ScriptKind.TS;
      parsed.set(file.path, ts.createSourceFile(file.path, source, ts.ScriptTarget.Latest, true, kind));
    } catch (error) {
      parsed.set(file.path, { error });
    }
  }
  return parsed;
}

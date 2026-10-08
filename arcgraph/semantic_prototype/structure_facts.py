"""Parse-only stdlib worker for explicit target interpreters, never project imports.

The worker is deliberately executable without ArcGraph or third-party packages.
Its structural profile is separate from the legacy oracle used in batch four.
"""

import ast
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tokenize

PROFILE = "structure-facts/0.1"


def identity(kind, *parts):
    data = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return kind + ":" + hashlib.sha256(data.encode()).hexdigest()[:24]


def location(node):
    return [node.lineno - 1, node.col_offset, node.end_lineno - 1, node.end_col_offset]


def source_text(raw):
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")


class Facts(ast.NodeVisitor):
    def __init__(self, path, text, version):
        self.path = path
        self.text = text
        self.lines = text.split("\n")
        self.raw = text.encode()
        self.offsets = []
        offset = 0
        for line in self.lines:
            self.offsets.append(offset)
            offset += len(line.encode()) + 1
        self.version = version
        self.records = []
        self.module = "module:" + path
        self.syntactic = self.module
        self.scope = self.module
        self.execution = self.module
        self.phase = "module_body"
        self.qualname = ""
        self.future = False

    @contextmanager
    def context(self, **values):
        old = {name: getattr(self, name) for name in values}
        for name, value in values.items():
            setattr(self, name, value)
        try:
            yield
        finally:
            for name, value in old.items():
                setattr(self, name, value)

    def expression(self, node):
        if node is None:
            return None
        return {
            "span": location(node),
            "source": self.segment(node),
            "syntax": type(node).__name__,
        }

    def segment(self, node):
        start = self.offsets[node.lineno - 1] + node.col_offset
        end = self.offsets[node.end_lineno - 1] + node.end_col_offset
        return self.raw[start:end].decode()

    def record(self, kind, node, payload, *, tag="", record_id=None):
        row = {
            "id": record_id or identity(kind, self.path, *location(node), tag),
            "kind": kind,
            "path": self.path,
            "span": location(node),
            "syntactic_owner": self.syntactic,
            "lexical_scope": self.scope,
            "execution_owner": self.execution,
            "phase": self.phase,
            "payload": payload,
        }
        self.records.append(row)
        return row

    def token(self, node):
        if isinstance(node, ast.Name):
            return location(node)
        if isinstance(node, ast.Attribute):
            prefix = self.lines[node.end_lineno - 1].encode()[: node.end_col_offset]
            prefix = prefix.decode()
            start = len(prefix)
            while start and ("a" + prefix[start - 1]).isidentifier():
                start -= 1
            return [
                node.end_lineno - 1,
                len(prefix[:start].encode()),
                node.end_lineno - 1,
                node.end_col_offset,
            ]
        return None

    def named_span(self, node):
        if type(node).__name__ == "TypeAlias":
            return location(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            segment = self.lines[node.lineno - 1].encode()[node.col_offset :].decode()
            match = re.match(r"(?:async\s+)?(?:def|class)\s+([^\s(:\[]+)", segment)
            start = node.col_offset + len(segment[: match.start(1)].encode())
            return [
                node.lineno - 1,
                start,
                node.lineno - 1,
                start + len(match.group(1).encode()),
            ]
        return location(node)

    def arguments(self, args):
        positional = [*args.posonlyargs, *args.args]
        defaults = [None] * (len(positional) - len(args.defaults)) + args.defaults
        rows = []
        for i, (arg, default) in enumerate(zip(positional, defaults)):
            kind = "positional_only" if i < len(args.posonlyargs) else "positional"
            rows.append(self.parameter(arg, kind, default))
        if args.vararg:
            rows.append(self.parameter(args.vararg, "vararg", None))
        rows.extend(
            self.parameter(arg, "keyword_only", default)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults)
        )
        if args.kwarg:
            rows.append(self.parameter(args.kwarg, "kwarg", None))
        return rows

    def parameter(self, arg, kind, default):
        return {
            "name": arg.arg,
            "kind": kind,
            "span": location(arg),
            "annotation": self.expression(arg.annotation),
            "default": self.expression(default),
            "type_comment": arg.type_comment,
        }

    def definition(self, node, kind, name):
        local = self.qualname + "." + name if self.qualname else name
        module = self.path[:-3].replace("/", ".").removesuffix(".__init__")
        payload = {
            "symbol_kind": kind,
            "name": name,
            "qualname": module + "." + local,
            "name_span": self.named_span(node),
            "decorators": [
                self.expression(d) for d in getattr(node, "decorator_list", [])
            ],
            "bases": [self.expression(b) for b in getattr(node, "bases", [])],
            "class_keywords": [
                {"name": k.arg, "value": self.expression(k.value)}
                for k in getattr(node, "keywords", [])
            ],
            "parameters": (
                self.arguments(node.args)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                else []
            ),
            "returns": self.expression(getattr(node, "returns", None)),
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "type_parameters": [
                {
                    "name": p.name,
                    "syntax": type(p).__name__,
                    "span": location(p),
                    "bound": self.expression(getattr(p, "bound", None)),
                    "default": self.expression(getattr(p, "default_value", None)),
                }
                for p in getattr(node, "type_params", [])
            ],
        }
        d = self.record(
            "definition",
            node,
            payload,
            record_id=identity("def", self.path, *location(node), kind),
        )
        return d, local

    def artificial_scope(self, node, kind, parent):
        scope = identity("scope", self.path, *location(node), kind)
        self.record(
            "scope", node, {"scope_kind": kind, "parent": parent}, record_id=scope
        )
        return scope

    def visit_many(self, nodes):
        for node in nodes:
            if node is not None:
                self.visit(node)

    def annotation(
        self, expression, node, definition, *, local=False, role="annotation"
    ):
        if expression is None:
            return
        phase = (
            "annotation_local_no_eval"
            if local
            else (
                "annotation_stringized"
                if self.future
                else (
                    "annotation_lazy" if self.version >= (3, 14) else "annotation_eager"
                )
            )
        )
        execution = self.execution
        lexical = self.scope
        if phase in ("annotation_local_no_eval", "annotation_stringized"):
            execution = None
        elif phase == "annotation_lazy":
            execution = self.artificial_scope(expression, "annotation", lexical)
            lexical = execution
        with self.context(
            syntactic=definition, execution=execution, scope=lexical, phase=phase
        ):
            self.record(
                "annotation",
                expression,
                {"role": role, "expression": self.expression(expression)},
            )
            self.visit(expression)

    def type_parameters(self, node, definition):
        params = getattr(node, "type_params", [])
        if not params:
            return self.scope
        scope = self.artificial_scope(node, "type_parameters", self.scope)
        with self.context(
            scope=scope,
            syntactic=definition,
            execution=scope,
            phase="type_parameter_lazy",
        ):
            for p in params:
                self.visit_many(
                    [getattr(p, "bound", None), getattr(p, "default_value", None)]
                )
        return scope

    def function(self, node):
        d, local = self.definition(node, "function", node.name)
        with self.context(syntactic=d["id"], phase="decorator"):
            self.visit_many(node.decorator_list)
        scope = self.type_parameters(node, d["id"])
        # Generic function defaults and decorators remain in the outer scope.
        with self.context(syntactic=d["id"], phase="definition_default"):
            self.visit_many([*node.args.defaults, *node.args.kw_defaults])
        execution = scope if getattr(node, "type_params", []) else self.execution
        with self.context(syntactic=d["id"], scope=scope, execution=execution):
            for arg in [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                node.args.vararg,
                node.args.kwarg,
            ]:
                if arg:
                    self.annotation(arg.annotation, node, d["id"], role="parameter")
            self.annotation(node.returns, node, d["id"], role="return")
        with self.context(
            syntactic=d["id"],
            scope=d["id"],
            execution=d["id"],
            phase="function_body",
            qualname=local,
        ):
            self.visit_many(node.body)

    visit_FunctionDef = function
    visit_AsyncFunctionDef = function

    def visit_ClassDef(self, node):
        d, local = self.definition(node, "class", node.name)
        with self.context(syntactic=d["id"], phase="decorator"):
            self.visit_many(node.decorator_list)
        scope = self.type_parameters(node, d["id"])
        execution = scope if getattr(node, "type_params", []) else self.execution
        with self.context(
            syntactic=d["id"], scope=scope, execution=execution, phase="definition_base"
        ):
            self.visit_many([*node.bases, *(k.value for k in node.keywords)])
        with self.context(
            syntactic=d["id"],
            scope=d["id"],
            execution=d["id"],
            phase="class_body",
            qualname=local,
        ):
            self.visit_many(node.body)

    def visit_Lambda(self, node):
        d, local = self.definition(node, "lambda", "<lambda>")
        with self.context(syntactic=d["id"], phase="definition_default"):
            self.visit_many([*node.args.defaults, *node.args.kw_defaults])
        with self.context(
            syntactic=d["id"],
            scope=d["id"],
            execution=d["id"],
            phase="lambda_body",
            qualname=local,
        ):
            self.visit(node.body)

    def comprehension(self, node):
        generator = isinstance(node, ast.GeneratorExp)
        if generator:
            d, _ = self.definition(node, "generator", "<genexpr>")
            scope = d["id"]
        else:
            scope = self.artificial_scope(node, type(node).__name__, self.scope)
        # The first iterable belongs to the surrounding lexical/execution scope.
        self.visit(node.generators[0].iter)
        execution = scope if generator or self.version < (3, 12) else self.execution
        phase = "deferred_generator" if generator else "comprehension_eager"
        with self.context(
            syntactic=scope, scope=scope, execution=execution, phase=phase
        ):
            for i, part in enumerate(node.generators):
                self.record(
                    "binding",
                    part.target,
                    {
                        "role": "comprehension_target",
                        "target": self.expression(part.target),
                    },
                )
                if i:
                    self.visit(part.iter)
                self.visit_many(part.ifs)
            if isinstance(node, ast.DictComp):
                self.visit_many([node.key, node.value])
            else:
                self.visit(node.elt)

    visit_GeneratorExp = comprehension
    visit_ListComp = comprehension
    visit_SetComp = comprehension
    visit_DictComp = comprehension

    def visit_TypeAlias(self, node):
        d, _ = self.definition(node, "type_alias", node.name.id)
        scope = self.type_parameters(node, d["id"])
        scope = self.artificial_scope(node.value, "type_alias", scope)
        with self.context(
            syntactic=d["id"], scope=scope, execution=scope, phase="type_alias_lazy"
        ):
            self.visit(node.value)

    def visit_Call(self, node):
        self.record(
            "callsite",
            node,
            {
                "callee_span": location(node.func),
                "token_span": self.token(node.func),
                "callee": self.expression(node.func),
                "expression": self.segment(node),
                "normalized_expression": ast.unparse(node),
                "arguments": [self.expression(a) for a in node.args],
                "keywords": [
                    {"name": k.arg, "value": self.expression(k.value)}
                    for k in node.keywords
                ],
                "execution": "syntax_only" if self.execution is None else "conditional",
            },
            record_id=identity("call", self.path, *location(node)),
        )
        self.generic_visit(node)

    def visit_Import(self, node):
        self.imports(node, None, 0)

    def visit_ImportFrom(self, node):
        self.imports(node, node.module, node.level)

    def imports(self, node, module, level):
        self.record(
            "import",
            node,
            {
                "module": module,
                "level": level,
                "aliases": [
                    {
                        "name": a.name,
                        "asname": a.asname,
                        "span": location(a),
                        "binding": (
                            None
                            if a.name == "*"
                            else a.asname
                            or (
                                a.name.split(".")[0]
                                if module is None and level == 0
                                else a.name
                            )
                        ),
                    }
                    for a in node.names
                ],
                "star": any(a.name == "*" for a in node.names),
            },
        )

    def binding(self, node):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        self.record(
            "binding",
            node,
            {
                "role": type(node).__name__,
                "targets": [self.expression(t) for t in targets],
                "value": self.expression(node.value),
                "annotation": self.expression(getattr(node, "annotation", None)),
                "operator": (
                    type(node.op).__name__ if isinstance(node, ast.AugAssign) else None
                ),
            },
        )
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            value = node.value
            static = isinstance(value, (ast.List, ast.Tuple, ast.Set)) and all(
                isinstance(v, ast.Constant) and isinstance(v.value, str)
                for v in value.elts
            )
            self.record(
                "export",
                node,
                {
                    "operation": type(node).__name__,
                    "status": "literal" if static else "dynamic",
                    "names": [v.value for v in value.elts] if static else [],
                },
            )
        self.visit_many(targets)
        self.visit_many([node.value])
        if isinstance(node, ast.AnnAssign):
            local = self.phase in (
                "function_body",
                "lambda_body",
                "comprehension_eager",
                "deferred_generator",
            )
            self.annotation(
                node.annotation, node, self.syntactic, local=local, role="variable"
            )

    visit_Assign = binding
    visit_AnnAssign = binding
    visit_AugAssign = binding
    visit_NamedExpr = binding

    def declarations(self, node):
        self.record("binding", node, {"role": type(node).__name__, "names": node.names})

    visit_Global = declarations
    visit_Nonlocal = declarations

    def generic_visit(self, node):
        if isinstance(
            node,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.With,
                ast.AsyncWith,
                ast.Try,
                ast.TryStar,
                ast.Match,
            ),
        ):
            self.record(
                "control",
                node,
                {
                    "syntax": type(node).__name__,
                    "expressions": {
                        key: self.expression(getattr(node, key, None))
                        for key in ("test", "iter", "target", "subject")
                    },
                    "with_items": [
                        {
                            "context": self.expression(i.context_expr),
                            "target": self.expression(i.optional_vars),
                        }
                        for i in getattr(node, "items", [])
                    ],
                },
            )
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self.record(
                "binding",
                node.target,
                {"role": "loop_target", "target": self.expression(node.target)},
            )
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars:
                    self.record(
                        "binding",
                        item.optional_vars,
                        {
                            "role": "with_target",
                            "target": self.expression(item.optional_vars),
                        },
                    )
        if isinstance(node, ast.ExceptHandler) and node.name:
            self.record(
                "binding", node, {"role": "exception_target", "name": node.name}
            )
        super().generic_visit(node)


def scan(root, paths, target):
    version = sys.version_info[:2]
    if sys.implementation.name != "cpython" or target != ".".join(map(str, version)):
        return {
            "profile": PROFILE,
            "interpreter": {
                "implementation": sys.implementation.name,
                "version": sys.version,
                "target": ".".join(map(str, version)),
            },
            "files": [],
            "records": [],
        }
    files = []
    records = []
    root = Path(root).resolve()
    for relative in paths:
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("source outside frozen root")
        raw = path.read_bytes()
        file = {
            "path": relative,
            "raw_digest": hashlib.sha256(raw).hexdigest(),
            "status": "parsed",
            "diagnostic": None,
        }
        try:
            text = source_text(raw)
            tree = ast.parse(text, relative, type_comments=True)
            visitor = Facts(relative, text, version)
            visitor.future = any(
                isinstance(n, ast.ImportFrom)
                and n.module == "__future__"
                and any(a.name == "annotations" for a in n.names)
                for n in tree.body
            )
            visitor.visit(tree)
            records.extend(visitor.records)
        except (SyntaxError, UnicodeError, ValueError, RecursionError) as exc:
            file["status"] = "parse_error"
            file["diagnostic"] = {
                "reason": type(exc).__name__,
                "message": str(exc),
                "line": getattr(exc, "lineno", None),
                "column": getattr(exc, "offset", None),
                "native_unit": "unicode_scalar",
                "recovery": "none",
            }
        files.append(file)
    return {
        "profile": PROFILE,
        "interpreter": {
            "implementation": sys.implementation.name,
            "version": sys.version,
            "target": target,
        },
        "files": files,
        "records": records,
    }


if __name__ == "__main__":
    request = json.load(sys.stdin)
    json.dump(
        scan(request["root"], request["paths"], request["target"]),
        sys.stdout,
        ensure_ascii=False,
    )

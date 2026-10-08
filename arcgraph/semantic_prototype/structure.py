"""Stdlib structural oracle, also executable by a specified local interpreter."""

import ast
import hashlib
import io
import json
import sys
import tokenize
import re
from pathlib import Path


def ident(kind, *parts):
    return (
        kind
        + ":"
        + hashlib.sha256(
            json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    )


def span(n):
    return [n.lineno - 1, n.col_offset, n.end_lineno - 1, n.end_col_offset]


def contains(a, b):
    return tuple(a[:2]) <= tuple(b[:2]) and tuple(b[2:]) <= tuple(a[2:])


def module_name(rel):
    name = rel[:-3].replace("/", ".")
    return name[:-9] if name.endswith(".__init__") else name


def read_source(path):
    raw = path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")


def token_span(func, lines):
    if isinstance(func, ast.Name):
        return span(func)
    if isinstance(func, ast.Attribute):
        prefix = lines[func.end_lineno - 1].encode()[: func.end_col_offset].decode()
        start = len(prefix)
        while start and ("a" + prefix[start - 1]).isidentifier():
            start -= 1
        return [
            func.end_lineno - 1,
            len(prefix[:start].encode()),
            func.end_lineno - 1,
            func.end_col_offset,
        ]
    return None


class Scan(ast.NodeVisitor):
    def __init__(self, rel, text):
        self.path = rel
        self.text = text
        self.lines = text.split("\n")
        self.module = module_name(rel)
        self.defs = []
        self.sites = []
        self.access = []
        self.stack = []
        self.phase = "module_body"
        self.callee_nodes = set()
        self.owner = "module:" + rel
        self.annotations = {}

    def defrecord(self, node, kind, name):
        parent = self.stack[-1] if self.stack else None
        localq = (parent["local_qualname"] + "." if parent else "") + name
        runtimeq = (
            parent["runtime_qualname"]
            + (".<locals>." if parent["kind"] in ["function", "lambda"] else ".")
            if parent
            else ""
        ) + name
        record = {
            "id": ident("def", self.path, *span(node), kind),
            "path": self.path,
            "span": span(node),
            "kind": kind,
            "name": name,
            "qualname": self.module + "." + localq,
            "local_qualname": localq,
            "runtime_qualname": runtimeq,
            "first_line": min(
                [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
            ),
            "line": node.lineno,
            "column": node.col_offset,
            "owner": self.owner,
            "decorators": [ast.unparse(d) for d in getattr(node, "decorator_list", [])],
            "bases": [ast.unparse(b) for b in getattr(node, "bases", [])],
        }
        record["name_span"] = span(node)
        if kind in ["function", "class"]:
            segment = self.lines[node.lineno - 1].encode()[node.col_offset :].decode()
            match = re.match(r"(?:async\s+)?(?:def|class)\s+([^\s(:\[]+)", segment)
            start = node.col_offset + len(segment[: match.start(1)].encode())
            record["name_span"] = [
                node.lineno - 1,
                start,
                node.lineno - 1,
                start + len(match.group(1).encode()),
            ]
        record["declaration_only"] = kind == "function" and (
            any(x.endswith("abstractmethod") for x in record["decorators"])
            or bool(
                parent
                and any(x.split("[")[0].endswith("Protocol") for x in parent["bases"])
            )
        )
        self.defs.append(record)
        return record

    def visit_phase(self, nodes, phase):
        old = self.phase
        self.phase = phase
        for n in nodes:
            if n is not None:
                self.visit(n)
        self.phase = old

    def visit_FunctionDef(self, node):
        self.function(node)

    def visit_AsyncFunctionDef(self, node):
        self.function(node)

    def function(self, node):
        d = self.defrecord(node, "function", node.name)
        self.visit_phase(node.decorator_list, "decorator")
        self.visit_phase(
            [*node.args.defaults, *node.args.kw_defaults], "definition_default"
        )
        self.visit_phase(
            [
                a.annotation
                for a in [
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                ]
                if a.annotation
            ]
            + [node.returns],
            "annotation_deferred",
        )
        oldowner = self.owner
        oldann = self.annotations
        self.owner = d["id"]
        self.stack.append(d)
        self.annotations = {
            a.arg: ast.unparse(a.annotation)
            for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if a.annotation
        }
        self.visit_phase(node.body, "function_body")
        self.stack.pop()
        self.owner = oldowner
        self.annotations = oldann

    def visit_ClassDef(self, node):
        d = self.defrecord(node, "class", node.name)
        self.visit_phase(node.decorator_list, "decorator")
        self.visit_phase([*node.bases, *[k.value for k in node.keywords]], "class_base")
        oldowner = self.owner
        self.owner = d["id"]
        self.stack.append(d)
        self.visit_phase(node.body, "class_body")
        self.stack.pop()
        self.owner = oldowner

    def visit_Lambda(self, node):
        d = self.defrecord(node, "lambda", "<lambda>")
        self.visit_phase(
            [*node.args.defaults, *node.args.kw_defaults], "definition_default"
        )
        oldowner = self.owner
        self.owner = d["id"]
        self.stack.append(d)
        self.visit_phase([node.body], "lambda_body")
        self.stack.pop()
        self.owner = oldowner

    def visit_GeneratorExp(self, node):
        d = self.defrecord(node, "generator", "<genexpr>")
        old = self.owner
        # First iterable is evaluated in the outer scope; body/filters on iteration.
        self.visit(node.generators[0].iter)
        self.owner = d["id"]
        self.stack.append(d)
        oldphase = self.phase
        self.phase = "deferred_generator"
        self.visit(node.elt)
        for i, g in enumerate(node.generators):
            if i:
                self.visit(g.iter)
            for cond in g.ifs:
                self.visit(cond)
        self.stack.pop()
        self.owner = old
        self.phase = oldphase

    def visit_Call(self, node):
        token = token_span(node.func, self.lines)
        self.callee_nodes.add(id(node.func))
        self.sites.append(
            {
                "id": ident("call", self.path, *span(node)),
                "path": self.path,
                "span": span(node),
                "callee_span": span(node.func),
                "token_span": token,
                "owner": self.owner,
                "phase": self.phase,
                "syntactic_owner": (
                    self.stack[-1]["id"] if self.stack else "module:" + self.path
                ),
                "name": ast.unparse(node.func),
                "expression": ast.unparse(node),
                "receiver_annotation": (
                    self.annotations.get(node.func.value.id)
                    if isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    else None
                ),
                "shape": (
                    "attribute"
                    if isinstance(node.func, ast.Attribute)
                    else "name" if isinstance(node.func, ast.Name) else "special"
                ),
                "query_position": (
                    token[:2]
                    if token
                    else [
                        node.func.end_lineno - 1,
                        max(node.func.end_col_offset - 1, 0),
                    ]
                ),
            }
        )
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if id(node) not in self.callee_nodes:
            self.access.append(
                {
                    "id": ident("access", self.path, *span(node)),
                    "path": self.path,
                    "span": span(node),
                    "token_span": token_span(node, self.lines),
                    "owner": self.owner,
                    "phase": self.phase,
                    "name": ast.unparse(node),
                    "expression": ast.unparse(node),
                    "access_kind": type(node.ctx).__name__,
                    "shape": "attribute",
                    "query_position": token_span(node, self.lines)[:2],
                }
            )
        self.generic_visit(node)


def scan(root, paths):
    definitions = []
    calls = []
    failures = []
    files = []
    for rel in paths:
        path = root / rel
        raw = path.read_bytes()
        files.append({"path": rel, "sha256": hashlib.sha256(raw).hexdigest()})
        try:
            text = read_source(path)
            tree = ast.parse(text, rel)
            visitor = Scan(rel, text)
            visitor.visit(tree)
        except (SyntaxError, UnicodeError, ValueError) as exc:
            failures.append((rel, str(exc)))
            continue
        definitions.extend(visitor.defs)
        calls.extend(visitor.sites)
    return {
        "python": sys.version,
        "files": files,
        "definitions": definitions,
        "callsites": calls,
        "parse_failures": failures,
    }


if __name__ == "__main__":
    request = json.load(sys.stdin)
    json.dump(
        scan(Path(request["root"]), request["paths"]), sys.stdout, ensure_ascii=False
    )

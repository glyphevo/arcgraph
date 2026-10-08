"""Conservative declaration evidence derived from frozen structural records."""

from collections import defaultdict
import re


def declaration_evidence(records):
    definitions = {r.id: r for r in records if r.kind == "definition"}
    classes = {
        k: r for k, r in definitions.items() if r.payload["symbol_kind"] == "class"
    }
    by_name = defaultdict(list)
    for r in classes.values():
        by_name[r.payload["qualname"]].append(r.id)
    imports = defaultdict(lambda: defaultdict(set))
    rebound = defaultdict(set)
    for r in records:
        if r.kind == "binding" and r.syntactic_owner == "module:" + r.path:
            for target in r.payload.get("targets", ()):
                if re.fullmatch(r"\w+", target["source"]):
                    rebound[r.path].add(target["source"])
        if (
            r.kind != "import"
            or r.payload.get("typing_only")
            or r.syntactic_owner != "module:" + r.path
        ):
            continue
        p = r.payload
        module = p["module"]
        if p["level"]:
            parent = r.path[:-3].replace("/", ".").split(".")[: -p["level"]]
            module = ".".join([*parent, *([module] if module else [])])
        for alias in p["aliases"]:
            if alias["binding"] is not None:
                target = (
                    (module + "." if module else "") + alias["name"]
                    if p["module"] is not None or p["level"]
                    else alias["name"] if alias["asname"] else alias["binding"]
                )
                imports[r.path][alias["binding"]].add(target)

    parents = defaultdict(set)
    for r in classes.values():
        module = r.path[:-3].replace("/", ".").removesuffix(".__init__")
        for base in r.payload["bases"]:
            name = base["source"].split("[")[0].strip()
            if not re.fullmatch(r"\w+(?:\.\w+)*", name):
                continue
            alias, _, tail = name.partition(".")
            if alias in rebound[r.path]:
                continue
            imported = imports[r.path].get(alias, set())
            if imported:
                names = {x + ("." + tail if tail else "") for x in imported}
            else:
                names = {module + "." + name}
            possible = [k for n in names for k in by_name.get(n, ())]
            if len(possible) == 1 and possible[0] != r.id:
                parents[r.id].add(possible[0])

    def ancestors(cls):
        seen, pending = set(), list(parents[cls])
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(parents[current] - seen)
        return seen

    evidence = {
        r.id: ("typing_only_definition",)
        for r in definitions.values()
        if r.payload.get("typing_only")
    }
    methods = defaultdict(list)
    for r in definitions.values():
        if r.payload["symbol_kind"] == "function" and r.syntactic_owner in classes:
            methods[(r.syntactic_owner, r.payload["name"])].append(r)
    for (cls, name), overrides in methods.items():
        for override in overrides:
            p = override.payload
            if (
                p.get("stub_body")
                or p.get("typing_only")
                or classes[cls].payload.get("typing_only")
                or any(
                    d["source"].split(".")[-1] == "abstractmethod"
                    for d in p["decorators"]
                )
            ):
                continue
            for parent in ancestors(cls):
                for base in methods.get((parent, name), ()):
                    if base.payload.get("stub_body") and base.id not in evidence:
                        evidence[base.id] = ("overridden_stub", override.id)
    return evidence

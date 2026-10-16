"""Documented methods of common concrete standard-library value classes.

Late constructor inference uses an explicit value contract, never capitalization
as proof of an opaque dependency's constructor or its arbitrary members.
"""

_DICT_METHODS = frozenset(
    {
        "clear",
        "copy",
        "fromkeys",
        "get",
        "items",
        "keys",
        "pop",
        "popitem",
        "setdefault",
        "update",
        "values",
    }
)
_STREAM_METHODS = frozenset(
    {
        "__enter__",
        "__exit__",
        "close",
        "detach",
        "fileno",
        "flush",
        "isatty",
        "read",
        "readable",
        "readinto",
        "readline",
        "readlines",
        "seek",
        "seekable",
        "tell",
        "truncate",
        "writable",
        "write",
        "writelines",
    }
)

CONSTRUCTOR_METHODS = {
    "extsym:collections.Counter": _DICT_METHODS
    | frozenset({"elements", "most_common", "subtract", "total"}),
    "extsym:collections.defaultdict": _DICT_METHODS | frozenset({"__missing__"}),
    "extsym:collections.deque": frozenset(
        {
            "append",
            "appendleft",
            "clear",
            "copy",
            "count",
            "extend",
            "extendleft",
            "index",
            "insert",
            "pop",
            "popleft",
            "remove",
            "reverse",
            "rotate",
        }
    ),
    "extsym:contextlib.ExitStack": frozenset(
        {
            "__enter__",
            "__exit__",
            "callback",
            "close",
            "enter_context",
            "pop_all",
            "push",
        }
    ),
    "extsym:threading.Event": frozenset({"clear", "is_set", "isSet", "set", "wait"}),
    "extsym:threading.Timer": frozenset(
        {
            "cancel",
            "getName",
            "is_alive",
            "isDaemon",
            "join",
            "run",
            "setDaemon",
            "setName",
            "start",
        }
    ),
    "extsym:subprocess.Popen": frozenset(
        {
            "__enter__",
            "__exit__",
            "communicate",
            "kill",
            "poll",
            "send_signal",
            "terminate",
            "wait",
        }
    ),
    **{
        f"extsym:{module}.BytesIO": _STREAM_METHODS
        | frozenset({"getbuffer", "getvalue", "read1", "readinto1"})
        for module in ("io", "_io")
    },
    **{
        f"extsym:{module}.TextIOWrapper": (_STREAM_METHODS - {"readinto"})
        | {"reconfigure"}
        for module in ("io", "_io")
    },
    **{
        f"extsym:{module}.StringIO": (_STREAM_METHODS - {"readinto"}) | {"getvalue"}
        for module in ("io", "_io")
    },
}

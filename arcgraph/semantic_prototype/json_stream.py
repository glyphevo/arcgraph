"""Canonical JSON without a second in-memory copy of an entire generation."""

import hashlib
import json
from pydantic import BaseModel


def chunks(value, exclude=frozenset()):
    if isinstance(value, BaseModel):
        if (
            "records" not in type(value).model_fields
            and "answers" not in type(value).model_fields
        ):
            yield json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            return
        fields = type(value).model_fields
        yield "{"
        for index, key in enumerate(sorted(set(fields) - set(exclude))):
            if index:
                yield ","
            yield json.dumps(key)
            yield ":"
            yield from chunks(getattr(value, key))
        yield "}"
    elif isinstance(value, dict):
        yield "{"
        for index, key in enumerate(sorted(value)):
            if not isinstance(key, str):
                raise ValueError("string JSON keys required")
            if index:
                yield ","
            yield json.dumps(key, ensure_ascii=False)
            yield ":"
            yield from chunks(value[key])
        yield "}"
    elif isinstance(value, (tuple, list)):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from chunks(item)
        yield "]"
    else:
        yield json.dumps(value, ensure_ascii=False, allow_nan=False)


def content_digest(value):
    result = hashlib.sha256()
    for part in chunks(value, {"complete", "count", "checksum"}):
        result.update(part.encode())
    return result.hexdigest()

"""Order-independent semantic digests alongside exact artifact checksums."""

from .contract import digest


def semantic_digest(generation):
    """Exclude attempt/envelope identities, retain every semantic/raw proposition.

    Artifact checksum validation remains separate and checks the complete bytes.
    Sequences with semantic order (search paths, parameters) are never reordered.
    Only record collections, candidate sets and mapping rows are canonical sets.
    """
    content = generation.model_dump(mode="json")
    for key in ("id", "complete", "count", "checksum", "structure"):
        content.pop(key, None)
    for name in ("answers", "records", "definitions", "callsites", "mapping"):
        rows = content.get(name)
        if rows is None:
            continue
        for row in rows:
            row.pop("envelope", None)
            if "candidates" in row:
                row["candidates"].sort(key=digest)
        rows.sort(key=digest)
    return digest(content)

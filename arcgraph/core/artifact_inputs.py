"""Optional captured artifact reads, retaining the original display paths."""

from io import BytesIO


def input_exists(owner, path):
    captured = getattr(owner, "_frozen_inputs", None)
    return captured.exists(path) if captured is not None else path.exists()


def input_text(owner, path):
    captured = getattr(owner, "_frozen_inputs", None)
    return (
        captured.read_bytes(path).decode("utf-8")
        if captured is not None
        else path.read_text(encoding="utf-8")
    )


def input_xml(owner, path):
    captured = getattr(owner, "_frozen_inputs", None)
    return BytesIO(captured.read_bytes(path)) if captured is not None else path


def input_stat(owner, path):
    captured = getattr(owner, "_frozen_inputs", None)
    return captured.stat(path) if captured is not None else path.stat()

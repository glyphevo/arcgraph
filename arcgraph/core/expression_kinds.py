"""Shared expression classification helpers."""

from __future__ import annotations


def call_expression_kind(raw_expression: str) -> str:
    if raw_expression.count(".") >= 2:
        return "chain"
    if "." in raw_expression:
        return "attribute"
    return "direct"

"""Portable benchmark inventory, execution, analysis, and reporting."""

from .schema import load_and_validate, validate_document

__all__ = ["load_and_validate", "validate_document"]

__version__ = "0.1.0"

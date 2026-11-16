"""Shared file documents, collections, links, contexts, mutations, and reconciliation."""

from .collections import DocumentCollections, validate_collection_name, validate_instruction_document
from .contexts import ContextReader
from .file_store import (
    DocumentFileStore,
    ensure_no_symlinks,
    render_markdown_document,
    validate_relative_markdown_path,
)
from .files import read_document, scan_markdown
from .link_codec import LinkCodec, document_root_relative_path, strip_wikilink_wrappers
from .links import collect_relations, direct_relations, resolve_required
from .mutations import DocumentMutations
from .projection import reconcile_index
from .reconcile import Reconciliation, SourceSnapshot, plan

__all__ = [
    "ContextReader",
    "DocumentCollections",
    "DocumentFileStore",
    "DocumentMutations",
    "LinkCodec",
    "Reconciliation",
    "SourceSnapshot",
    "collect_relations",
    "direct_relations",
    "document_root_relative_path",
    "ensure_no_symlinks",
    "plan",
    "read_document",
    "reconcile_index",
    "render_markdown_document",
    "resolve_required",
    "scan_markdown",
    "strip_wikilink_wrappers",
    "validate_collection_name",
    "validate_instruction_document",
    "validate_relative_markdown_path",
]

"""Archival recall — session passage archival and hybrid BM25/cosine recall.

Archives session transcript chunks and code passages (chunked, embedded,
ranked) into the Atelier passage store.  Used by ``MemoryService.recall()``
and directly by the engine and SDK.

For user-created named facts see ``memory.service.MemoryService``.
For code-structure indexing see ``semantic_file_memory``.
"""

from atelier.core.capabilities.archival_recall.capability import ArchivalRecallCapability
from atelier.core.capabilities.archival_recall.ranking import RankedPassage, rank_archival_passages
from atelier.core.capabilities.archival_recall.symbol_recall import SymbolRecallCapability

__all__ = ["ArchivalRecallCapability", "RankedPassage", "SymbolRecallCapability", "rank_archival_passages"]

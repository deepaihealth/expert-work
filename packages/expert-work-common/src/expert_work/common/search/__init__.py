"""Shared search primitives — Capability Uplift Sprint #6 (Mini-ADR U-6).

Pure algorithms with no persistence dependencies. Both the J.5
``KnowledgeRetriever`` and the new memory hybrid recall path import
from here so the implementation is single-sourced.
"""

from expert_work.common.search.decay import (
    DECAY_FLOOR,
    DEFAULT_HALF_LIFE,
    temporal_decay_factor,
)
from expert_work.common.search.mmr import DEFAULT_MMR_LAMBDA, mmr_select
from expert_work.common.search.rrf import rrf_fuse, rrf_fuse_scored, weighted_rrf_fuse_scored

__all__ = [
    "DECAY_FLOOR",
    "DEFAULT_HALF_LIFE",
    "DEFAULT_MMR_LAMBDA",
    "mmr_select",
    "rrf_fuse",
    "rrf_fuse_scored",
    "temporal_decay_factor",
    "weighted_rrf_fuse_scored",
]

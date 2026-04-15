"""
Compatibility wrapper for the shared PTM snapshot helper functions.
"""

from utils.model_snapshot_utils import (
    build_model_name_to_id,
    collect_frameworks_from_analyzer,
    extract_snapshot_occurrences_with_analyzer,
    load_signatures_for_imports,
    resolve_param_to_full_name,
)

__all__ = [
    "build_model_name_to_id",
    "collect_frameworks_from_analyzer",
    "extract_snapshot_occurrences_with_analyzer",
    "load_signatures_for_imports",
    "resolve_param_to_full_name",
]
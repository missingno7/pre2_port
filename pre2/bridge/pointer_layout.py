"""The pointer swizzle — re-export shim.

**Moved to :mod:`pre2.native.pointer_layout` on 2026-07-16** (Stage 2.5 of
docs/pre2/offset_free_release_plan.md). It was not separable from the layout in
:mod:`pre2.native.graph_layout`, which had to move so the product can construct its own object graph without
importing ``pre2.bridge``: this module needs graph_layout's pool constants, and graph_layout's
``_obj_from_image``/``_RefSwizzle`` lazily import this one — a cycle that only resolves if both live on the
same side.

Kept here as a re-export so existing bridge-side and test importers keep working unchanged. See that module
and docs/pre2/pointer_swizzle_design.md for the real thing.
"""
from __future__ import annotations

from pre2.native.pointer_layout import (  # noqa: F401
    ASSET_REGIONS, POOL_REGIONS, _STRIDE, from_offset, to_offset,
)

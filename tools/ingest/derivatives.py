"""Wire the HEIC->JPEG media derivative into the catalog after a verified
original has been committed. Runs only after ingestion of the original
already succeeded; a derivative failure is recorded and never undoes or
blocks that ingestion (docs/13-dump-first-ingestion.md).
"""

from __future__ import annotations

from pathlib import Path

from tools.ingest.catalog import Catalog
from tools.ingest.config import HeicToJpegConfig
from tools.ingest.identity import derivative_asset_id_for
from tools.ingest.media.heic import HeicToJpegOptions, convert_heic_to_jpeg, is_heic
from tools.ingest.media.interface import DerivativeResult
from tools.ingest.t7 import ResolvedT7

HEIC_TO_JPEG_KIND = "heic_to_jpeg"


def maybe_generate_heic_derivative(
    catalog: Catalog,
    resolved_t7: ResolvedT7,
    *,
    parent_asset_id: str,
    parent_final_path: Path,
    source_volume_id: str,
    source_class: str,
    config: HeicToJpegConfig,
) -> DerivativeResult | None:
    if not config.enabled or not is_heic(parent_final_path):
        return None

    existing = catalog.get_derivative(parent_asset_id, HEIC_TO_JPEG_KIND)
    if existing is not None and existing["status"] == "ok":
        return None  # idempotent: never re-encode, never produce photo-2.jpg

    child_asset_id = derivative_asset_id_for(parent_asset_id, HEIC_TO_JPEG_KIND)
    output_path = parent_final_path.with_suffix(".jpg")

    options = HeicToJpegOptions(
        jpeg_quality=config.jpeg_quality,
        preserve_metadata=config.preserve_metadata,
        preserve_icc_profile=config.preserve_icc_profile,
        normalize_orientation=config.normalize_orientation,
    )
    result = convert_heic_to_jpeg(parent_final_path, output_path, options)

    if result.success:
        assert result.sha256 is not None and result.size is not None and result.output_path is not None
        catalog.upsert_blob(result.sha256, size=result.size, mime="image/jpeg")
        catalog.upsert_derivative_asset(
            child_asset_id,
            parent_asset_id=parent_asset_id,
            source_volume_id=source_volume_id,
            source_class=source_class,
            sha256=result.sha256,
            size=result.size,
            mime="image/jpeg",
            final_path=result.output_path,
        )
        catalog.upsert_derivative(
            child_asset_id,
            parent_asset_id=parent_asset_id,
            kind=HEIC_TO_JPEG_KIND,
            converter=result.converter,
            converter_version=result.converter_version,
            quality=options.jpeg_quality,
            status="ok",
        )
        catalog.write_sidecar(child_asset_id, resolved_t7.sidecars_dir)
    else:
        # No child file was produced, so there is no child asset row to
        # reference -- record the failure against the parent only.
        catalog.upsert_derivative(
            None,
            parent_asset_id=parent_asset_id,
            kind=HEIC_TO_JPEG_KIND,
            converter=result.converter,
            converter_version=result.converter_version,
            quality=options.jpeg_quality,
            status="failed",
            error_message=result.error_message,
        )

    catalog.write_sidecar(parent_asset_id, resolved_t7.sidecars_dir)
    return result

"""
Server-side archive manifest service.
Inspects hosted archives once and caches pre-computed byte-offset manifests.
"""

from pathlib import Path
from typing import Dict, Optional
from liveextract.core.manifest import ArchiveManifest, generate_manifest_from_zip


class ManifestService:
    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir).resolve()
        self._cache: Dict[str, ArchiveManifest] = {}

    def get_manifest(self, archive_filename: str) -> Optional[ArchiveManifest]:
        if archive_filename in self._cache:
            return self._cache[archive_filename]

        archive_path = self.storage_dir / archive_filename
        if not archive_path.exists() or not archive_path.is_file():
            return None

        manifest = generate_manifest_from_zip(archive_path)
        self._cache[archive_filename] = manifest
        return manifest

    def clear_cache(self) -> None:
        self._cache.clear()

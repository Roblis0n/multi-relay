"""Multi Relay lifecycle and catalog primitives."""

from .branding import (
    CLI_NAME,
    MANAGEMENT_PREFIX,
    OWNERSHIP_MARKER,
    PACKAGE_NAME,
    PRODUCT_NAME,
    PRODUCT_VERSION,
    REPOSITORY_NAME,
)

from .errors import ManagerError
from .migration import (
    CatalogMigrationResult,
    migrate_catalog_1_to_2,
    migrate_catalog_file,
)
from .paths import Paths, resolve_paths
from .manager import RelayManager

__all__ = [
    "CLI_NAME",
    "CatalogMigrationResult",
    "ManagerError",
    "MANAGEMENT_PREFIX",
    "OWNERSHIP_MARKER",
    "PACKAGE_NAME",
    "Paths",
    "PRODUCT_NAME",
    "PRODUCT_VERSION",
    "REPOSITORY_NAME",
    "RelayManager",
    "migrate_catalog_1_to_2",
    "migrate_catalog_file",
    "resolve_paths",
]

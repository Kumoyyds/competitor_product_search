"""Cross-module product type aliases.

The qualified product contract remains owned by ``src.scraping``. Re-exporting
it here gives shared callers one stable import without duplicating its schema.
"""

from src.scraping.models import ProductData

__all__ = ["ProductData"]

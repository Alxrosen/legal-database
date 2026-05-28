"""Normalization utilities — comparison forms for raw scraped data."""

from legal_sourcing.normalize.address import NormalizedAddress, normalize_address
from legal_sourcing.normalize.name import NormalizedName, normalize_firm_name
from legal_sourcing.normalize.phone import normalize_phone
from legal_sourcing.normalize.practice_areas import (
    Taxonomy,
    TaxonomyArea,
    get_taxonomy,
    load_taxonomy,
    normalize_practice_area,
)
from legal_sourcing.normalize.title_rank import classify_title
from legal_sourcing.normalize.url import normalize_url

__all__ = [
    "NormalizedAddress",
    "NormalizedName",
    "Taxonomy",
    "TaxonomyArea",
    "classify_title",
    "get_taxonomy",
    "load_taxonomy",
    "normalize_address",
    "normalize_firm_name",
    "normalize_phone",
    "normalize_practice_area",
    "normalize_url",
]

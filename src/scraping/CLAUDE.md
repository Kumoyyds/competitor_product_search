# Scraping Module

**Status**: Skeleton (not yet implemented)

## Responsibility

Extracts structured product data from marketplace pages found by the search module. Provides parsed product attributes (title, price, brand, specifications) for downstream matching.

## Planned Structure

- `parsers/` — Marketplace-specific HTML parsers (e.g., Amazon, Tesco)

## Inputs / Outputs

- **Input**: Product URLs from search module
- **Output**: Structured product data (parsed attributes)

# API Module

**Status**: Skeleton (not yet implemented)

## Responsibility

REST API layer for triggering searches, checking progress, and retrieving results programmatically. Future web UI backend.

## Planned Structure

- `routes/` — API endpoint definitions
- `schemas/` — Request/response validation schemas (Pydantic)

## Inputs / Outputs

- **Input**: HTTP requests with SKU data and search parameters
- **Output**: JSON responses with search results, status updates

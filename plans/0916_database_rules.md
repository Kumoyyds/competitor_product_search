# Database Rules

When creating or reviewing the database:

1. Keep the schema minimal and based on actual application requirements.
2. Separate task-level results from node-level execution/debug records.
3. Store final status, final output, errors, and timestamps at task level.
4. Store node name, status, attempt, input/output, errors, and timestamps for each node execution.
5. Preserve execution history; do not overwrite failed attempts or retries.
6. Use explicit columns for fields that are frequently filtered, joined, sorted, or aggregated.
7. Use JSON for flexible node-specific or debug information.
8. Use appropriate primary keys, foreign keys, constraints, and indexes.
9. Ensure every node execution can be traced back to its task/run.
10. Avoid unnecessary tables, duplicate fields, speculative fields, and excessive logging.
11. Avoid storing very large raw artifacts in the main database unless necessary.
12. Prefer simple, incremental schema changes over unnecessary redesigns.

When reviewing a schema, focus on:

- correctness
- traceability
- debuggability
- query efficiency
- simplicity
- maintainability

Do not optimize for theoretical database purity. Prefer the simplest design that reliably supports the real workflow.
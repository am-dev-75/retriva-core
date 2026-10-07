# Spec 031 — Architecture

Status: PROPOSED. Governing: ADR-036. Repository: retriva-core.

## 1. Candidate selection (read-only)

Eligible superseded versions are selected in PostgreSQL (privileged operator
transaction), never inferred from legacy JSON:

```
SELECT v.version_id, v.document_id, v.tenant_id, v.superseded_at
FROM knowledge.document_versions v
JOIN knowledge.documents d ON d.document_id = v.document_id
WHERE v.status = 'superseded'
  AND (v.tenant_id = %s OR %s IS NULL)
  AND v.superseded_at IS NOT NULL
  AND v.superseded_at <= now() - make_interval(days => %s)   -- retention
  AND d.lifecycle_state = 'active'
  AND d.current_version_id IS DISTINCT FROM v.version_id
  AND v.provenance <> 'adopted_uncertain'        -- per D4 policy
  AND NOT EXISTS (open qdrant_operations for v.version_id)
LIMIT batch
```

Per candidate, the exact point set is the manifest (`version_chunks`,
`sync_state='verified'`), never a Qdrant filter alone.

## 2. Dry-run

Read-only: resolve candidates, count manifest points, classify blocked-by-reason
(serving/staging/open-op/rollback-hold/retention/provenance/missing-manifest),
compute age buckets and storage estimate, and produce a candidate fingerprint
(stable hash over sorted point ids). No writes.

## 3. Apply (bounded)

1. Recompute candidates and revalidate immediately before issue.
2. Insert `qdrant_operations` row `op_type='delete_points'`, state `prepared`,
   with idempotency key + candidate fingerprint + expected count.
3. Re-read each candidate point in Qdrant; abort a candidate if it is `serving`,
   belongs to the current version, or is missing from the manifest.
4. `client.delete_points(collection, ids=[...])` (explicit ids only).
5. Persist `executing` (before issue) → `applied_unverified` (after API
   acceptance). API acceptance is NOT completion.
6. Read-only zero-point postcondition for the exact candidate set; verify current
   serving points and retrieval unaffected.
7. Close `verified`. On ambiguity: `reconciliation_required` (never blind replay).

## 4. Ordering / interruption

- Candidate selection and the intent row are PostgreSQL-authoritative.
- If a crash occurs after `executing`/before evidence, reconciliation classifies
  the operation as ambiguous and re-runs the read-only zero-point check; it does
  not replay deletion blindly.
- Supersession-before-deactivation gap: reconciliation adds
  `superseded_but_serving`; authoritative retrieval already excludes non-current
  versions; the repair deactivates (`serving=false`) then deletes under the same
  operation model.

## 5. Idempotency

Idempotency key = `superseded_delete:{tenant}:{collection}:{version}` (one
in-flight op per version). A duplicate request returns the existing operation.
Deleted points are removed from the manifest; a re-run sees them as
`sync_state='removed'` and is a no-op.

## 6. Observability

Low-cardinality counters: candidates, eligible, blocked_by_reason, deleted,
verified, ambiguous, reclaimed_bytes_estimate. No tenant/document/version/point
ids in logs or metrics beyond bounded safe hashes.

## 7. Testing

Real isolated PostgreSQL + real Qdrant; deterministic candidate fixtures; the
spec §3 matrix incl. interruption/crash/retry/duplicate/restart/Redis reconnect;
zero-point postcondition; current/staging never eligible; cross-tenant isolation.

## 8. Migration

None. Reuses `qdrant_operations` (`delete_points`) and its states.

## 9. Operational bounds B1-B7 (implemented)

`src/retriva/knowledge/superseded_cleanup.py::CleanupBounds` carries B1-B7 and a
stable `fingerprint()`. `SupersededCleanup.discover/dry_run` are read-only;
`apply` enforces B5 (global active-op check) and B4 (durable rate limit from
`qdrant_operations.executed_at`) before discovery, then B7 (backlog OR p95,
fail-closed on missing evidence), revalidates the candidate fingerprint, caps at
B1 versions, and per version issues an explicit-ID delete (B2) with a bounded
B3 verification that never replays blindly.

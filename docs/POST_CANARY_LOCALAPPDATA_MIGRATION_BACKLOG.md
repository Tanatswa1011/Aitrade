# Post-canary `%LOCALAPPDATA%\AITRADE` migration backlog

This migration is required technical debt after the Phase 55D canary. It is not implemented during Recovery F.

## Target architecture

- Use one authoritative resolver for every mutable runtime boundary.
- Store production state, durable safety latches, OAuth material, and journals in separate subdirectories beneath `%LOCALAPPDATA%\AITRADE`.
- Keep all production mutable paths outside Git and OneDrive.
- Apply process-token SID ACLs to sensitive files and their atomic-replacement directories; never use ambient usernames or broad grants.
- Use same-directory temporary files, flush/close, `os.replace`, and post-write verification for atomic persistence.
- Hold a runtime lock containing PID, process start time, schema, and instance ID. Refuse a second writer.
- Maintain a manifest containing schema versions, canonical paths, sizes, and SHA-256 values for migrated bytes.
- Refuse startup on split-brain evidence, conflicting manifests, two writable roots, or ambiguous lock ownership.
- Prohibit tests from resolving or writing `%LOCALAPPDATA%\AITRADE`; tests must require a unique authoritative temporary root.

## Migration procedure

1. Stop the dashboard/runtime gracefully while keeping execution disarmed.
2. Fingerprint repository runtime files and verify all critical latches.
3. Create the LocalAppData directory tree with process-SID ACLs.
4. Acquire the migration/runtime lock.
5. Copy each approved file to a temporary destination, verify byte count and SHA-256, then atomically replace its final destination.
6. Write and verify the manifest.
7. Start in read-only, fail-closed mode and confirm one authoritative root only.
8. Preserve source files unchanged until a reviewed cutover is accepted.

## Rollback procedure

1. Stop the new runtime and preserve its manifest and diagnostics.
2. Do not merge divergent state or choose a newer file automatically.
3. Verify the original source fingerprints and latch integrity.
4. Restore the resolver configuration only after operator review.
5. Restart disarmed and require manual revalidation.

Rollback must never clear one-shot, completion, serious-failure, or manual-revalidation latches.

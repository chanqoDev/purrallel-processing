# Tablekeeper Stage 2 plan

## Goal

Extend the existing Stage 1 service in `stage-2/` to meet the complete Tablekeeper Stage 2 specification while keeping `stage-1/` independently buildable and preserving prior room exports.

## Acceptance criteria

- Both stage directories have a standalone `Dockerfile` and `RUN.md`; Stage 2 builds and starts with `PORT` and has no runtime network dependency.
- Stage 1 behavior remains supported, including authentication, idempotency, reservations, amendments, atomic moves, reset, export, and import.
- Stage 2 supports approved two-table combinations in availability, bookings, amendments, moves, cancellation, and responses with specified order, capacities, and atomic occupancy.
- Stage 1 exports import into Stage 2 while preserving the signed-in token, reservation references, and idempotency receipts needed to recover an uncertain booking without reload.
- Browser routes, required `data-testid` elements, latest-search ordering, table-unavailable recovery, and uncertain same-key/same-body retry match the specification.
- The experience remains usable at 375 CSS pixels without page-level horizontal scrolling and provides the specified visible states and keyboard-accessible controls.
- The unchanged official harness passes Stage 1 and Stage 2 in isolated mode against the exact committed revision.

## Dependencies and assignments

1. **Builder** owns implementation under `stage-2/`, keeps `stage-1/` and `room.json` intact, performs local checks, and commits the implementation.
2. **Verifier** waits for the commit, independently reviews that exact revision, then runs the official isolated Stage 1 + Stage 2 harness from `/Users/chris/Projects/dark-factory-wearedevs` using its supplied virtualenv and Docker.
3. **Builder** fixes reproducible findings in a new commit; **Verifier** reruns the official harness against the final commit and records the exact command, counts, and evidence paths.

## Evidence to record

Record the full final commit hash, the exact harness command, test counts/outcome, and absolute paths to `report.json`, `stage-1.log`, `stage-2.log`, and the counts JSON files. Existing Stage 1 evidence and room exports remain preserved.

## Authoritative inputs

- `/Users/chris/Projects/dark-factory-wearedevs/tablekeeper/spec/stage-1.md`
- `/Users/chris/Projects/dark-factory-wearedevs/tablekeeper/spec/stage-2.md`
- `FACTORY.md` and the role mandates in `mandates/`

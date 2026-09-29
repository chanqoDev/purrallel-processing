# Tablekeeper Factory

## Scope

This factory currently documents **Stage 1** of Tablekeeper in `stage-1/`. The repository root also contains the generic role mandates in `mandates/`. Keep stage implementation files inside their stage directory; keep the official challenge harness separate from the submission repository.

The service is packaged with a `Dockerfile` and its run instructions are in `stage-1/RUN.md`. The challenge requires a buildable service that does not rely on outbound network access at runtime. `RUN.md` is the source of truth for build and launch commands.

## Team and roles

The BAND room used three Codex seats:

- **Planner** — turns the supplied stage task into acceptance criteria and work assignments.
- **Builder** — implements the assigned change, runs local checks, commits it, and addresses review findings.
- **Verifier** — independently inspects the exact revision, runs the official harness, and reports reproducible evidence.

The role instructions are stored in `mandates/purrallel-planner.md`, `mandates/chris-builder.md`, and `mandates/purrallel-verifier.md`. They are generic operating instructions; stage-specific requirements come from the official task and harness.

## Stage 1 workflow

1. Read the official Stage 1 specification and inspect the existing repository before proposing changes.
2. Have the Planner publish acceptance criteria, dependencies, and file ownership in the BAND room.
3. Have the Builder implement only the assigned work under `stage-1/`, update `RUN.md` when needed, and commit the completed revision.
4. Have the Verifier check out or inspect that exact commit independently, build and exercise the service, and run the official harness in isolated mode.
5. Fix any reproducible findings, commit the fixes, and rerun the harness against the new exact commit. Record the final commit and evidence in the room.

Example official harness invocation (run from the challenge checkout, with the harness virtual environment installed):

```sh
.venv/bin/python -m harness run \
  --track tablekeeper \
  --build /tmp/band-hackathon-f62bd9a/stage-1 \
  --stages 1 \
  --mode isolated \
  --out /tmp/harness-tablekeeper-stage1-f62bd9a
```

The `--build` directory must contain the submitted Stage 1 build context expected by the harness. Use the challenge checkout's `harness/` and the stage's `RUN.md` as the authority if the harness interface changes.

## Design rationale

- **Containerized service:** a Docker build gives the evaluator a repeatable way to build and run the stage without depending on the developer's host setup.
- **Offline runtime:** avoiding outbound runtime dependencies makes evaluation more predictable and satisfies the challenge constraint.
- **Separate planner, builder, and verifier responsibilities:** planning defines the acceptance target, implementation owns changes, and independent verification checks the result against the actual task rather than relying on the builder's summary.
- **Commit-based verification:** recording the exact commit makes test evidence traceable to the revision being submitted and lets failures be reproduced.

## Verified Stage 1 evidence

The Verifier reported a successful official isolated harness run against commit `f62bd9a8b4ef57f27c425594aa50228ddc7d86f7`:

- 120 tests collected; 120 passed; 0 failed, 0 errors, 0 skipped.
- Harness output was recorded under `/tmp/harness-tablekeeper-stage1-f62bd9a` (`report.json`, `stage-1.log`, and `stage-1.counts.json`).
- The recorded run used the official `tablekeeper` track, Stage 1, and isolated mode.

The Builder also reported a Docker smoke check covering batch movement and exact replay, plus a 50-concurrent-login check completing in 1.836 seconds under a 2 CPU / 2 GiB limit. These are team-reported measurements; retain the underlying command output or harness artifacts with the submission if available.

Earlier, an attempt against commit `68b5a0825501a0e3231316a6759b05674afbeeb1` reportedly passed 114 of 120 tests, with six authentication timeouts. The team used that result to identify and fix the issue, then reran the official harness against the later commit listed above. Keep both the failed and passing evidence when available so the recovery is reproducible.

## Cost and autonomy record

- **Model/runtime:** Codex app-server through BAND-owned runtimes; the team selected `gpt-6-luna` after the ChatGPT-account configuration rejected other model IDs.
- **Measured inference/API cost:** not recorded in the evidence available for this document. Add a value only after checking the relevant BAND usage records; do not infer provider billing from local token estimates.
- **Human intervention:** this development run included human steering, runtime permission approvals, and a human-triggered harness rerun. It therefore does **not** demonstrate a no-intervention autonomous run. Do not describe it as one. If the submission claims autonomy, run a separate clean trial where the stage task is the only human input and preserve the room recording and logs.

## Remaining submission evidence

Before submission, add or link the final BAND room export, a video recording that visibly includes the BAND Desktop room, and exact cost measurements if available. The desktop room view was reported unusable during this work, so a qualifying room recording has not been verified here. Do not claim those artifacts are complete until they have been checked.

## Verified Stage 2 development checkpoint

Final implementation revision: `c70558396df518b8447eff1b5287023640c24fbf`.

The completed isolated harness report records:
- Stage 1: 120/120 passed.
- Stage 2: 25/25 passed, including 17 UI checks.
- Zero failures, errors, skips, or deselections.
- Highest contiguous passing stage: 2.

Evidence: `evidence/harness-tablekeeper-stage2-verifier-c705583/`.
Stage 2 plan: `plan.md`.
Development continuation room: `ba043fc6-dc3d-4b23-b292-5d79befa4ded`.

This development run included human permission approvals and runtime
restarts. These passing results establish implementation correctness
against the exercised checks, not a no-intervention autonomous run.

# Workflow validation policy

Validation remains in project-specific workflows. Each required aggregate rejects failed, cancelled or skipped dependencies. Sonar input generation runs without secrets; analysis consumes the triggering run’s artifact and trusted scanner configuration.

CodeRabbit automatic and incremental push reviews are disabled. Only after all validation checks succeed, including for Dependabot, may the coordinator or a maintainer request review. `Review passed` requires approval on the current head and must not be satisfied by merely requesting a review. Rerun the coordinator for the current SHA after resolving findings.

## Repository settings

The intended required checks are recorded in `workflow-policy.json`. Default workflow tokens must be read-only, security updates enabled, and actions SHA-pinned. Preserve existing review, conversation, force-push and deletion protections.

The `workflow_run` coordinator and scanner use the default-branch workflow. The PR introducing them cannot exercise its own privileged code. Keep the existing protection during bootstrap; activate `Review passed` only after the workflow is on main and a hosted run has produced it. Do not bypass failed analysis or reviews.

## Bootstrap and recovery

After the Test workflow passes on this PR, dispatch the Sonar workflow on the PR branch with `source_run_id` set to that successful run. It verifies the source workflow, result, revision and current PR before scanning. Dispatch `coderabbit-after-checks.yml` on the PR branch with its current head `revision`; the coordinator loads policy from its own workflow revision. This tests the privileged flow before the first merge without disabling protection.

## Apply the matching repository policy

After merging, run `python3 .github/scripts/apply-workflow-policy.py` to inspect the exact ruleset change, then run it with `--apply`. The script refuses to apply an unmerged policy, preserves unrelated rules and bypass actors, binds checks to GitHub Actions, enables SHA enforcement and the replacement automation, and verifies the result. Repository settings shared with the existing workflows can be corrected before merge; new check names and action enforcement must wait for their producing workflows.

## Validation-gated label requester

The coordinator uses the built-in GitHub Actions token to apply the `review-ready` label only after validation succeeds. CodeRabbit is configured for that positive label with automatic and incremental push reviews disabled. The normal token therefore has Issues write permission for label changes; the coordinator remains checkout-free and never approves PRs.

This token-free route is undergoing hosted verification in this PR. The optional `REVIEW_REQUEST_TOKEN` comment requester remains available for compatibility. Current-head CodeRabbit approval is required whichever route starts the review.

The migration accepts only an explicit main-only ruleset; shared branch targets require a separate main policy. Metadata-only review notifications re-evaluate readiness and revoked approvals. Polling is bounded to 45 one-minute attempts.

Review command markers are deduplicated per requester, revision and run attempt. A requester may post at most three requests for one revision. Once exhausted, a maintainer requests review manually; after current-head CodeRabbit approval arrives, rerunning the coordinator can pass. A new revision resets the count.

The required `Review notifier policy` commit status is written by a checkout-free `pull_request_target` worker from the default branch. It checks the complete notification structure, regular Git file modes, and the trusted enforcement/receiver files as data. Pinned action updates must belong to the official action repository. Changes to security-policy logic need a maintainer-reviewed trusted baseline update; the PR cannot rewrite its own automatic validator. The coordinator requires the actual status, so a same-named PR check cannot replace it.

### Trusted notifier bootstrap and recovery

After the harmless PR registration job has registered the workflow, the repository owner must inspect the exact workflow revision and dispatch `review-notifier-policy.yml` on the reviewed PR branch with `revision` set to the full current head SHA. Wait for its successful `Review notifier policy` status before dispatching the coordinator. This initial dispatch is an explicit owner attestation of the reviewed baseline; arbitrary writers are not authorized to attest a feature-branch validator. Normal validation uses the default-branch `pull_request_target` workflow. A status from another workflow, revision, failed run or untrusted feature-branch actor must not satisfy this gate.

Privileged Sonar workers perform static source/report analysis only, with `sonar.sca.enabled=false` preventing implicit dependency/build-tool execution. Fork-triggered CI never receives the scanner token; the trusted receiver consumes validated source and coverage without running fork-selected commands. Existing dependency audits and Dependabot protection remain independent. Every configured coverage report must be present before scanning.

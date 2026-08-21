# Payload Snapshot: 5.0.0-0.ci-2026-07-11-223154

OpenShift 5.0 ci (amd64) — **Rejected**

## Quick Start

Read `summary.json` first. It contains everything you need for
triage: job states, failure streaks, test regressions, build-log
error counts, and relative paths to all detailed data files.
Only drill into per-job or per-PR files when you need specifics.

## This Snapshot

- **Target payload**: `5.0.0-0.ci-2026-07-11-223154`
- **Phase**: Rejected
- **Blocking jobs**: 3/5 failed
- **Informing jobs**: 0/0 failed
- **Chain depth**: 8 payloads back to baseline
- **Baseline**: `5.0.0-0.ci-2026-07-06-025117` (139.7h ago)

### Payload chain (newest first)

  - 5.0.0-0.ci-2026-07-11-223154
  - 5.0.0-0.ci-2026-07-11-163154
  - 5.0.0-0.ci-2026-07-11-103154
  - 5.0.0-0.ci-2026-07-11-043154
  - 5.0.0-0.ci-2026-07-10-191205
  - 5.0.0-0.ci-2026-07-10-071205
  - 5.0.0-0.ci-2026-07-08-051042
  - 5.0.0-0.ci-2026-07-06-025117

### Failed blocking jobs

  - `aggregated-aws-ovn-upgrade-5.0-major`
  - `aggregated-gcp-ovn-upgrade-5.0-micro`
  - `hypershift-e2e-aks`

## File Layout

```
5.0/ci/
  summary.json              # START HERE — full triage data
  CLAUDE.md                  # This file
  streams.json              # All streams for this OCP version
  5.0.0-0.ci-2026-07-11-223154/                    # Target payload
    payload.json            # Release controller API response
    changelog.json          # PRs changed vs previous payload
    regressions.json        # Test failure regression tracking
    jobs/
      blocking/
        <job-name>/
          job.json          # State, prow URL, GCS URL, retries
          build_log.json    # Error/warning lines + log tail
          junit/
            results.json    # Parsed test failures
            *.xml           # Raw JUnit XML
      informing/
        <job-name>/
          job.json          # State and URLs only (no junit)
    <component>/
      prs/
        <number>/
          code.diff         # Full git diff
          comments.json     # PR comments and reviews
          jobs.json         # CI check runs
    rpmdb/                    # RPMDB from RHCOS images
      rhel-coreos/           # queryable with rpm --dbpath
        rpmdb.sqlite
      rhel-coreos-10/
        rpmdb.sqlite
  <older-payload-tag>/      # Each prior payload in the chain
    ...                     # Same structure
```

## Key Concepts

- **Blocking vs informing**: Only blocking job failures prevent
  payload acceptance. Informing jobs are tracked but don't block.
- **Chain**: The sequence of payloads walking backwards from the
  target until one where all blocking jobs passed (the baseline).
- **Streaks**: Per-job consecutive failure count from the target
  backwards. `failure_pattern` shows the full history (F=fail,
  S=succeed) across the chain.
- **Regressions**: Per-test tracking — when did each test failure
  first appear? `first_failed_in` identifies the originating
  payload, `payloads_failing` counts how many payloads it spans.
- **Build log**: Error/warning lines extracted from the Prow
  build-log.txt, plus the last 20% of the log for context.

## summary.json Schema

Top-level fields:
- `payload_tag`, `phase`, `release_url`, `architecture`,
  `stream`, `version`
- `chain_length`, `baseline_tag`, `hours_since_baseline`
- `blocking_jobs.failed_jobs[]` — each entry has: `name`,
  `state`, `prow_url`, `gcs_url`, `streak` (with
  `streak_length`, `originating_payload`, `is_new_failure`,
  `failure_pattern`), `build_log_errors`, `test_failure_count`,
  and relative paths to `job_json`, `junit_results`, `build_log`
- `informing_jobs.failed_jobs[]` — job name strings only
- `test_failures.blocking[]` — `test_name`, `jobs`,
  `first_failed_in`, `payloads_failing`, `failure_message`,
  `failure_text`
- `payloads[]` — per-payload entries with `tag`, `phase`,
  relative paths, `prs[]` with component/diff/comments paths,
  `rhcos_changes[]` with RPM diffs per RHCOS variant, and
  `rhcos_rpms[]` with rpmdb.sqlite per variant
- `rhcos_rpms[]` — RPMDB per RHCOS variant for the target
  payload: `tag`, `name`, `pullspec`, `rpmdb` (relative path
  to rpmdb.sqlite — queryable with rpm --dbpath <dir> or sqlite3)

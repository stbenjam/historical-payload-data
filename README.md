# Historical Payload Data

Archived OpenShift CI payload snapshots for use by [ai-helpers](https://github.com/openshift-eng/ai-helpers) evals. Each top-level directory is a payload snapshot containing release controller data, job results, JUnit XML, PR diffs, and regression tracking.

Job artifacts are stored in the `prow-artifact-archive` GCS bucket and referenced by the snapshot data.

## Adding a new snapshot

### 1. Get the snapshot from the payload agent's CI job

The snapshot is produced by the payload analysis agent's Prow job for each release payload. Download it from the job's GCS artifacts:

```bash
# Find the payload agent job for the release payload in question,
# then download its snapshot from the job artifacts
gcloud storage cp -r \
  gs://test-platform-results/<job-path>/artifacts/<step>/artifacts/<payload-tag>/ \
  .
```

### 2. Archive referenced jobs to GCS

> **Note:** Write access to the `prow-artifact-archive` GCS bucket is required. If you don't have access, ask someone with permissions to run this step for you.

The snapshot references Prow job artifacts by URL. Archive them to the `prow-artifact-archive` bucket so they remain available after the original bucket expires:

```bash
python3 hack/archive-job.py --from-snapshot <payload-tag>/ --parallel 8
```

The script will:
- Extract all job paths from the snapshot's JSON files
- Copy every artifact for every job from `test-platform-results` to `prow-artifact-archive` via server-side GCS copy; artifacts are not filtered or minimized
- Treat `test-platform-results` as strictly read-only, with a code-level guard against using it as a write target
- Rewrite only each regular job's `prowjob.json` locally
- Inspect aggregate index jobs (`aggregated-*`, `aggregator-*`, and `*-analysis-all`), rewrite their references, and recursively archive every dependent job in full
- Write `.archive-complete.json` only after the server-side copy finishes

Jobs with a completion marker are skipped automatically. An unmarked job is
re-copied server-side when its source still exists, repairing interrupted and
legacy copies without downloading their artifact trees. If the source has
already expired, the unmarked destination is retained and clearly reported
rather than being presented as verified.

### 3. Commit the snapshot

```bash
cp -r <snapshot-dir> .
git add <payload-tag>/
git commit -m "Add <payload-tag> snapshot for eval case-NNN"
```

## hack/archive-job.py

Reusable script for copying Prow jobs between GCS buckets with reference rewriting.

```
usage: archive-job.py [-h] [--from-file FILE] [--from-snapshot DIR]
                      [--dry-run] [--no-recursive] [--parallel N]
                      [--fix-prowjobs] [--rewrite] [jobs ...]

  jobs                      Job path(s), e.g. logs/<job-name>/<build-id>
  --from-file, -f FILE      Read job paths from a file (one per line)
  --from-snapshot, -s DIR   Extract and archive all jobs from a snapshot directory
  --dry-run, -n             Print what would be archived without doing it
  --no-recursive            Don't recursively archive referenced jobs
  --parallel, -p N          Number of jobs to archive in parallel (default: 4)
  --fix-prowjobs            Rewrite prowjob.json for all jobs in dest bucket
  --rewrite                 Rewrite aggregate metadata or prowjob.json in
                            already-archived jobs
```

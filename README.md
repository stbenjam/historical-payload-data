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

The snapshot references Prow job artifacts by URL. Archive them to the `prow-artifact-archive` bucket so they remain available after the original bucket expires:

```bash
# Extract job paths from the snapshot and archive them
grep -rh '"gcs_bucket_path"' <snapshot-dir> | \
  sed 's/.*"gcs_bucket_path": "prow-artifact-archive\//logs\//' | \
  sed 's/".*//' | sort -u > /tmp/jobs-to-archive.txt

python3 hack/archive-job.py --from-file /tmp/jobs-to-archive.txt --parallel 8
```

The script will:
- Copy each job's artifacts from `test-platform-results` to `prow-artifact-archive` via server-side GCS copy
- Rewrite `prowjob.json` to reference the new bucket
- For `aggregated-*` jobs, rewrite all text files and recursively archive dependent jobs

Jobs already in `prow-artifact-archive` are skipped automatically, so re-running is safe.

### 3. Commit the snapshot

```bash
cp -r <snapshot-dir> .
git add <payload-tag>/
git commit -m "Add <payload-tag> snapshot for eval case-NNN"
```

## hack/archive-job.py

Reusable script for copying Prow jobs between GCS buckets with reference rewriting.

```
usage: archive-job.py [-h] [--from-file FILE] [--dry-run] [--no-recursive]
                      [--parallel N] [--fix-prowjobs]
                      [jobs ...]

  jobs                  Job path(s), e.g. logs/<job-name>/<build-id>
  --from-file, -f FILE  Read job paths from a file (one per line)
  --dry-run, -n         Print what would be archived without doing it
  --no-recursive        Don't recursively archive referenced jobs
  --parallel, -p N      Number of jobs to archive in parallel (default: 4)
  --fix-prowjobs        Rewrite prowjob.json for all jobs in dest bucket
```

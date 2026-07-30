#!/usr/bin/env python3
"""Archive Prow jobs from test-platform-results to prow-artifact-archive.

Copies job artifacts via server-side GCS copy. For aggregated-* jobs, also
rewrites bucket references and recursively archives dependent jobs.
"""

import argparse
import concurrent.futures
import functools
import glob
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

print = functools.partial(print, flush=True)

SOURCE_BUCKET = "test-platform-results"
DEST_BUCKET = "prow-artifact-archive"
COMPLETE_MARKER = ".archive-complete.json"

JOB_PATH_RE = re.compile(
    r"(?:test-platform-results|prow-artifact-archive)/((?:logs|pr-logs)/[a-zA-Z0-9/._-]+?/\d{16,})"
)

_archived_lock = threading.Lock()


def _is_source_url(value):
    return value == f"gs://{SOURCE_BUCKET}" or value.startswith(f"gs://{SOURCE_BUCKET}/")


def _assert_source_bucket_read_only(args):
    """Reject commands that could write to the source bucket."""
    source_urls = [arg for arg in args if _is_source_url(arg)]
    if not source_urls:
        return

    command = args[0]
    if command in {"cat", "ls"}:
        return

    # A bucket-to-bucket copy reads from every argument except its final
    # destination. The source bucket must never be that destination.
    if command == "cp" and not _is_source_url(args[-1]):
        return

    raise ValueError(
        f"refusing command that could modify read-only bucket {SOURCE_BUCKET}: "
        f"gcloud storage {' '.join(args)}"
    )


def gcs_run(*args, check=False):
    _assert_source_bucket_read_only(args)
    cmd = ["gcloud", "storage"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def job_exists_in_dest(job_path):
    result = gcs_run("ls", f"gs://{DEST_BUCKET}/{job_path}/started.json")
    return result.returncode == 0


def job_is_complete_in_dest(job_path):
    result = gcs_run("ls", f"gs://{DEST_BUCKET}/{job_path}/{COMPLETE_MARKER}")
    return result.returncode == 0


def job_exists_in_source(job_path):
    result = gcs_run("ls", f"gs://{SOURCE_BUCKET}/{job_path}/started.json")
    return result.returncode == 0


def server_side_copy(job_path):
    """Copy job between buckets server-side (no local download)."""
    result = gcs_run(
        "cp", "-r",
        f"gs://{SOURCE_BUCKET}/{job_path}/*",
        f"gs://{DEST_BUCKET}/{job_path}/",
    )
    if result.returncode != 0:
        print(f"  ERROR copying: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def mark_job_complete(job_path):
    """Record that the full server-side copy finished successfully."""
    marker = {
        "version": 1,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": f"gs://{SOURCE_BUCKET}/{job_path}/",
        "destination": f"gs://{DEST_BUCKET}/{job_path}/",
    }
    marker_path = f"gs://{DEST_BUCKET}/{job_path}/{COMPLETE_MARKER}"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(marker, f, indent=2, sort_keys=True)
        f.write("\n")
        tmppath = f.name
    try:
        result = gcs_run("cp", tmppath, marker_path)
    finally:
        os.unlink(tmppath)
    if result.returncode != 0:
        print(
            f"  ERROR writing completion marker: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def is_aggregated(job_path):
    job_name = job_path.split("/")[1] if "/" in job_path else ""
    return (
        job_name.startswith(("aggregated-", "aggregator-"))
        or job_name.endswith("-analysis-all")
    )


def rewrite_and_find_refs(job_path):
    """Rewrite an aggregate job and return every referenced child job.

    Aggregate jobs are small indexes over many much larger child jobs. Download
    the aggregate index so references can be discovered in any text artifact,
    but upload only the individual files that actually changed. Child jobs are
    copied in full server-side and never pass through this function.
    """
    referenced = set()
    with tempfile.TemporaryDirectory() as tmpdir:
        result = gcs_run(
            "cp", "-r",
            f"gs://{DEST_BUCKET}/{job_path}/*",
            f"{tmpdir}/",
        )
        if result.returncode != 0:
            print(f"  ERROR downloading for rewrite: {result.stderr.strip()}", file=sys.stderr)
            return referenced

        modified_files = []
        for root, _dirs, files in os.walk(tmpdir):
            for fname in files:
                fpath = os.path.join(root, fname)
                if os.path.relpath(fpath, tmpdir) == COMPLETE_MARKER:
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8", errors="strict") as f:
                        content = f.read()
                except (UnicodeDecodeError, ValueError):
                    continue

                for m in JOB_PATH_RE.finditer(content):
                    referenced.add(m.group(1))

                rewritten = content.replace(SOURCE_BUCKET, DEST_BUCKET)
                if rewritten != content:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(rewritten)
                    modified_files.append(fpath)

        for fpath in modified_files:
            relative_path = os.path.relpath(fpath, tmpdir)
            result = gcs_run(
                "cp",
                fpath,
                f"gs://{DEST_BUCKET}/{job_path}/{relative_path}",
            )
            if result.returncode != 0:
                print(
                    f"  ERROR re-uploading {relative_path}: {result.stderr.strip()}",
                    file=sys.stderr,
                )

    return referenced


def rewrite_prowjob(job_path):
    """Rewrite just prowjob.json in dest bucket to fix bucket references."""
    gcs_path = f"gs://{DEST_BUCKET}/{job_path}/prowjob.json"
    result = gcs_run("cat", gcs_path)
    if result.returncode != 0:
        return
    content = result.stdout
    rewritten = content.replace(SOURCE_BUCKET, DEST_BUCKET)
    if rewritten != content:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(rewritten)
            tmppath = f.name
        try:
            gcs_run("cp", tmppath, gcs_path)
        finally:
            os.unlink(tmppath)


def archive_job(job_path, archived, dry_run=False, recursive=True, rewrite_only=False,
                executor=None, pending_futures=None):
    with _archived_lock:
        if job_path in archived:
            return
        archived.add(job_path)

    try:
        _archive_job_inner(job_path, archived, dry_run, recursive, rewrite_only, executor, pending_futures)
    except Exception as e:
        print(f"  ERROR ({job_path}): {e}", file=sys.stderr)


def _archive_job_inner(job_path, archived, dry_run, recursive, rewrite_only, executor, pending_futures):
    complete_in_dest = job_is_complete_in_dest(job_path)
    exists_in_dest = complete_in_dest or job_exists_in_dest(job_path)

    if rewrite_only:
        if not exists_in_dest:
            print(f"  SKIP (not in dest): {job_path}")
            return
        if dry_run:
            print(f"  WOULD REWRITE: {job_path}")
            return
        print(f"  REWRITING: {job_path}")
        if is_aggregated(job_path):
            rewrite_and_find_refs(job_path)
        else:
            rewrite_prowjob(job_path)
        print(f"  DONE: {job_path}")
        return

    if complete_in_dest:
        print(f"  SKIP (already archived): {job_path}")
        if dry_run:
            return
        if recursive and is_aggregated(job_path):
            referenced = rewrite_and_find_refs(job_path)
            _queue_refs(referenced, archived, dry_run, recursive, rewrite_only, executor, pending_futures)
        elif not is_aggregated(job_path):
            rewrite_prowjob(job_path)
        return

    exists_in_source = job_exists_in_source(job_path)
    if not exists_in_source and exists_in_dest:
        print(f"  WARNING (unmarked archive; source unavailable): {job_path}")
        if dry_run:
            return
        referenced = set()
        if is_aggregated(job_path):
            referenced = rewrite_and_find_refs(job_path)
        else:
            rewrite_prowjob(job_path)
        if recursive and referenced:
            _queue_refs(
                referenced,
                archived,
                dry_run,
                recursive,
                rewrite_only,
                executor,
                pending_futures,
            )
        return

    if not exists_in_source:
        print(f"  SKIP (not in source or dest): {job_path}")
        return

    if dry_run:
        action = "REPAIR" if exists_in_dest else "ARCHIVE"
        print(f"  WOULD {action}: {job_path}")
        return

    action = "REPAIRING" if exists_in_dest else "ARCHIVING"
    print(f"  {action}: {job_path}")

    if not server_side_copy(job_path):
        return

    referenced = set()
    if is_aggregated(job_path):
        referenced = rewrite_and_find_refs(job_path)
    else:
        rewrite_prowjob(job_path)

    if not mark_job_complete(job_path):
        return

    print(f"  DONE: {job_path}")

    if recursive and referenced:
        _queue_refs(referenced, archived, dry_run, recursive, rewrite_only, executor, pending_futures)


def _queue_refs(referenced, archived, dry_run, recursive, rewrite_only, executor, pending_futures=None):
    with _archived_lock:
        new_refs = referenced - archived
    if not new_refs:
        return
    print(f"  Found {len(new_refs)} new referenced job(s) to archive")
    if executor and pending_futures is not None:
        for ref_path in sorted(new_refs):
            pending_futures.put(executor.submit(
                archive_job, ref_path, archived,
                dry_run=dry_run, recursive=recursive, rewrite_only=rewrite_only,
                executor=executor, pending_futures=pending_futures,
            ))
    else:
        for ref_path in sorted(new_refs):
            archive_job(ref_path, archived, dry_run=dry_run, recursive=recursive,
                        rewrite_only=rewrite_only)


def normalize_path(path):
    """Strip bucket name, gs:// prefix, or URL components to get a bare job path."""
    path = path.strip().rstrip("/")
    for prefix in [
        f"gs://{SOURCE_BUCKET}/",
        f"{SOURCE_BUCKET}/",
        f"https://prow.ci.openshift.org/view/gs/{SOURCE_BUCKET}/",
        f"https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/{SOURCE_BUCKET}/",
    ]:
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    return path.rstrip("/")


def extract_jobs_from_snapshot(snapshot_dir):
    """Extract job paths from a payload snapshot directory."""
    job_paths = set()
    for filepath in glob.glob(os.path.join(snapshot_dir, "**", "*.json"), recursive=True):
        try:
            with open(filepath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get("gcs_bucket_path"):
            raw = data["gcs_bucket_path"]
            for bucket in (SOURCE_BUCKET, DEST_BUCKET):
                if raw.startswith(bucket + "/"):
                    raw = raw[len(bucket) + 1:]
                    break
            if raw:
                job_paths.add(raw)
    return sorted(job_paths)


def fix_all_prowjobs(parallel):
    """Find and rewrite all prowjob.json files in the dest bucket that still reference the old bucket."""
    print("Listing all prowjob.json files in dest bucket...")
    result = gcs_run("ls", f"gs://{DEST_BUCKET}/logs/*/*/prowjob.json")
    if result.returncode != 0:
        print(f"ERROR listing: {result.stderr.strip()}", file=sys.stderr)
        return

    all_paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    print(f"Found {len(all_paths)} prowjob.json files")

    fixed = []
    fixed_lock = threading.Lock()

    def check_and_fix(gcs_path):
        r = gcs_run("cat", gcs_path)
        if r.returncode != 0 or SOURCE_BUCKET not in r.stdout:
            return
        rewritten = r.stdout.replace(SOURCE_BUCKET, DEST_BUCKET)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(rewritten)
            tmppath = f.name
        try:
            gcs_run("cp", tmppath, gcs_path)
        finally:
            os.unlink(tmppath)
        with fixed_lock:
            fixed.append(gcs_path)
            if len(fixed) % 50 == 0:
                print(f"  Fixed {len(fixed)} so far...")

    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = [executor.submit(check_and_fix, p) for p in all_paths]
        for f in concurrent.futures.as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)

    elapsed = time.time() - start
    print(f"\nDone. Fixed {len(fixed)} of {len(all_paths)} prowjob.json files in {elapsed:.0f}s.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "jobs", nargs="*",
        help="Job path(s), e.g. logs/<job-name>/<build-id>",
    )
    parser.add_argument(
        "--from-file", "-f",
        help="Read job paths from a file (one per line)",
    )
    parser.add_argument(
        "--from-snapshot", "-s",
        help="Extract and archive all jobs referenced in a payload snapshot directory",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Print what would be archived without doing it",
    )
    parser.add_argument(
        "--no-recursive", action="store_true",
        help="Don't recursively archive referenced jobs",
    )
    parser.add_argument(
        "--parallel", "-p", type=int, default=4,
        help="Number of jobs to archive in parallel (default: 4)",
    )
    parser.add_argument(
        "--fix-prowjobs", action="store_true",
        help="Rewrite prowjob.json for all jobs in dest bucket to fix old bucket references",
    )
    parser.add_argument(
        "--rewrite", action="store_true",
        help=(
            "Rewrite aggregate metadata or prowjob.json in already-archived jobs "
            "(use with job paths or --from-snapshot)"
        ),
    )
    args = parser.parse_args()

    if args.fix_prowjobs:
        fix_all_prowjobs(args.parallel)
        return

    job_paths = []
    for j in args.jobs or []:
        job_paths.append(normalize_path(j))

    if args.from_file:
        with open(args.from_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    job_paths.append(normalize_path(line))

    if args.from_snapshot:
        snapshot_jobs = extract_jobs_from_snapshot(args.from_snapshot)
        print(f"Found {len(snapshot_jobs)} jobs in snapshot {args.from_snapshot}")
        job_paths.extend(snapshot_jobs)

    if not job_paths:
        parser.error("No job paths specified. Provide paths as arguments or use --from-file.")

    archived = set()
    total = len(job_paths)
    start = time.time()

    pending = queue.Queue()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as executor:
        for i, job_path in enumerate(job_paths, 1):
            print(f"[{i}/{total}] {job_path}")
            pending.put(executor.submit(
                archive_job, job_path, archived,
                dry_run=args.dry_run,
                recursive=not args.no_recursive,
                rewrite_only=args.rewrite,
                executor=executor,
                pending_futures=pending,
            ))
        while not pending.empty():
            f = pending.get()
            f.result()

    elapsed = time.time() - start
    print(f"\nDone. {len(archived)} job(s) processed in {elapsed:.0f}s.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import importlib.util
import pathlib
import subprocess
import unittest
from unittest import mock


SCRIPT_PATH = pathlib.Path(__file__).with_name("archive-job.py")
SPEC = importlib.util.spec_from_file_location("archive_job", SCRIPT_PATH)
archive_job = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive_job)


def completed(*args, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class SourceBucketSafetyTests(unittest.TestCase):
    @mock.patch.object(archive_job.subprocess, "run")
    def test_rejects_copy_to_source_bucket(self, run):
        with self.assertRaisesRegex(ValueError, "read-only bucket"):
            archive_job.gcs_run(
                "cp",
                "/tmp/prowjob.json",
                "gs://test-platform-results/logs/job/1234567890123456/prowjob.json",
            )
        run.assert_not_called()

    @mock.patch.object(archive_job.subprocess, "run")
    def test_allows_server_side_copy_from_source_to_archive(self, run):
        run.return_value = completed()

        archive_job.gcs_run(
            "cp",
            "-r",
            "gs://test-platform-results/logs/job/1234567890123456/*",
            "gs://prow-artifact-archive/logs/job/1234567890123456/",
        )

        run.assert_called_once()

    @mock.patch.object(archive_job.subprocess, "run")
    def test_rejects_source_bucket_mutation_commands(self, run):
        for command in ("rm", "mv"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "read-only bucket"):
                    archive_job.gcs_run(
                        command,
                        "gs://test-platform-results/logs/job/1234567890123456",
                    )
        run.assert_not_called()


class AggregationClassificationTests(unittest.TestCase):
    def test_recognizes_all_known_aggregation_job_families(self):
        aggregation_jobs = (
            "logs/aggregated-gcp-release-analysis-aggregator/1234567890123456",
            "logs/aggregator-periodic-ci-upgrade/1234567890123456",
            "logs/periodic-ci-openshift-release-main-nightly-5.0-install-analysis-all/"
            "1234567890123456",
            "logs/periodic-ci-openshift-release-main-nightly-5.0-overall-analysis-all/"
            "1234567890123456",
            "logs/periodic-ci-openshift-release-main-nightly-5.0-upgrade-analysis-all/"
            "1234567890123456",
        )
        for job_path in aggregation_jobs:
            with self.subTest(job_path=job_path):
                self.assertTrue(archive_job.is_aggregated(job_path))

    def test_regular_child_job_is_not_aggregate(self):
        self.assertFalse(
            archive_job.is_aggregated(
                "logs/periodic-ci-openshift-release-main-ci-5.0-e2e-aws-ovn/"
                "1234567890123456"
            )
        )


class RewriteTests(unittest.TestCase):
    @mock.patch.object(archive_job, "gcs_run")
    def test_aggregate_rewrite_uploads_only_modified_files(self, gcs_run):
        calls = []

        def run(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("cp", "-r"):
                local_dir = pathlib.Path(args[-1])
                (local_dir / "artifacts").mkdir()
                (local_dir / "artifacts" / "junit.xml").write_text(
                    "https://prow.ci.openshift.org/view/gs/test-platform-results/"
                    "logs/child-job/1234567890123456"
                )
                (local_dir / "unchanged.log").write_text("complete haystack")
            return completed(*args)

        gcs_run.side_effect = run
        job_path = "logs/aggregated-job/9999999999999999"

        referenced = archive_job.rewrite_and_find_refs(job_path)

        self.assertEqual(
            referenced, {"logs/child-job/1234567890123456"}
        )
        uploads = [
            args for args in calls
            if args[0] == "cp" and args[:2] != ("cp", "-r")
        ]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(
            uploads[0][-1],
            "gs://prow-artifact-archive/logs/aggregated-job/9999999999999999/"
            "artifacts/junit.xml",
        )
        self.assertNotIn("unchanged.log", uploads[0][-1])

    @mock.patch.object(archive_job, "gcs_run")
    def test_prowjob_rewrite_only_writes_to_archive(self, gcs_run):
        gcs_run.side_effect = (
            completed(
                stdout=(
                    '{"url":"gs://test-platform-results/logs/job/'
                    '1234567890123456"}'
                )
            ),
            completed(),
        )

        archive_job.rewrite_prowjob("logs/job/1234567890123456")

        cat_call, upload_call = [call.args for call in gcs_run.call_args_list]
        self.assertEqual(cat_call[0], "cat")
        self.assertTrue(cat_call[-1].startswith("gs://prow-artifact-archive/"))
        self.assertEqual(upload_call[0], "cp")
        self.assertTrue(upload_call[-1].startswith("gs://prow-artifact-archive/"))
        self.assertNotIn("gs://test-platform-results/", upload_call[-1])


class ArchiveFlowTests(unittest.TestCase):
    @mock.patch.object(archive_job, "_queue_refs")
    @mock.patch.object(archive_job, "rewrite_and_find_refs")
    @mock.patch.object(archive_job, "rewrite_prowjob")
    @mock.patch.object(archive_job, "server_side_copy")
    @mock.patch.object(archive_job, "job_exists_in_source")
    @mock.patch.object(archive_job, "job_exists_in_dest", return_value=True)
    def test_dry_run_never_rewrites_existing_archive(
        self,
        _exists_dest,
        exists_source,
        server_side_copy,
        rewrite_prowjob,
        rewrite_and_find_refs,
        queue_refs,
    ):
        archive_job._archive_job_inner(
            "logs/aggregated-gcp-job/9999999999999999",
            set(),
            True,
            True,
            False,
            None,
            None,
        )

        exists_source.assert_not_called()
        server_side_copy.assert_not_called()
        rewrite_prowjob.assert_not_called()
        rewrite_and_find_refs.assert_not_called()
        queue_refs.assert_not_called()

    @mock.patch.object(archive_job, "_queue_refs")
    @mock.patch.object(archive_job, "rewrite_and_find_refs")
    @mock.patch.object(archive_job, "rewrite_prowjob")
    @mock.patch.object(archive_job, "server_side_copy", return_value=True)
    @mock.patch.object(archive_job, "job_exists_in_source", return_value=True)
    @mock.patch.object(archive_job, "job_exists_in_dest", return_value=False)
    def test_regular_job_never_uses_recursive_rewrite(
        self,
        _exists_dest,
        _exists_source,
        _copy,
        rewrite_prowjob,
        rewrite_and_find_refs,
        queue_refs,
    ):
        job_path = "logs/periodic-ci-e2e/1234567890123456"

        archive_job._archive_job_inner(
            job_path, set(), False, True, False, None, None
        )

        rewrite_prowjob.assert_called_once_with(job_path)
        rewrite_and_find_refs.assert_not_called()
        queue_refs.assert_not_called()

    @mock.patch.object(archive_job, "_queue_refs")
    @mock.patch.object(
        archive_job,
        "rewrite_and_find_refs",
        return_value={"logs/child/1234567890123456"},
    )
    @mock.patch.object(archive_job, "rewrite_prowjob")
    @mock.patch.object(archive_job, "server_side_copy", return_value=True)
    @mock.patch.object(archive_job, "job_exists_in_source", return_value=True)
    @mock.patch.object(archive_job, "job_exists_in_dest", return_value=False)
    def test_aggregate_discovers_children_after_server_side_copy(
        self,
        _exists_dest,
        _exists_source,
        _copy,
        rewrite_prowjob,
        rewrite_and_find_refs,
        queue_refs,
    ):
        job_path = "logs/aggregated-gcp-job/9999999999999999"

        archive_job._archive_job_inner(
            job_path, set(), False, True, False, None, None
        )

        rewrite_and_find_refs.assert_called_once_with(job_path)
        rewrite_prowjob.assert_not_called()
        queue_refs.assert_called_once()


if __name__ == "__main__":
    unittest.main()

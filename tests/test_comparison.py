"""Baseline comparison must not turn missing or mismatched evidence into a pass."""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from kcidev import KernelCIClient

from kci_patchwork.__main__ import report_summary
from kci_patchwork.report import render
from kci_patchwork.results import collect
from kci_patchwork.workflow import checkout_snapshot

API = "https://staging.example.test"
BASE_ID, ROOT_ID = "1" * 24, "2" * 24
BASE_TREE, TREE = "a" * 64, "b" * 64
JOB = "kbuild-example"


def node(number, name, kind, parent, path, result="pass", data=None):
    return {
        "id": f"{number:024x}",
        "name": name,
        "kind": kind,
        "parent": parent,
        "path": path,
        "state": "done",
        "result": result,
        "treeid": BASE_TREE,
        "data": data or {},
        "artifacts": {},
    }


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        http = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected HTTP"),
        )
        http.start()
        self.addCleanup(http.stop)
        self.base = {
            "id": BASE_ID,
            "treeid": BASE_TREE,
            "name": "checkout",
            "kind": "checkout",
            "state": "done",
            "result": "pass",
            "parent": None,
            "path": ["checkout"],
            "artifacts": {"tarball": "https://files.example.test/source.tar.gz"},
            "data": {
                "kernel_revision": {
                    "url": "https://example.test/linux.git",
                    "branch": "master",
                    "commit": "c" * 40,
                }
            },
        }
        self.root = deepcopy(self.base) | {
            "id": ROOT_ID,
            "name": "patchset",
            "parent": BASE_ID,
            "treeid": TREE,
            "path": ["checkout", "patchset"],
            "jobfilter": [JOB],
            "platform_filter": [],
        }
        self.root["data"]["kernel_revision"]["patchset"] = "e" * 64
        self.manifest = {
            "checkout": checkout_snapshot(self.base, BASE_ID),
            "endpoints": {"api": API, "pipeline": "https://pipeline.example.test"},
            "expected_pipeline_patchset_hash": "e" * 64,
            "job_filter": [JOB],
            "platform_filter": [],
            "patches": [],
            "series": {"id": 42, "name": "Test series", "version": 1},
        }
        self.state = {
            "status": "submitted",
            "treeid": TREE,
            "patchset_node_id": ROOT_ID,
        }
        build = node(
            10,
            JOB,
            "kbuild",
            BASE_ID,
            ["checkout", JOB],
            data={
                "arch": "x86_64",
                "compiler": "gcc-14",
                "config_full": "tinyconfig+kselftest",
                "platform": "kubernetes",
                "fragments": ["kselftest"],
            },
        )
        group = node(11, "selftests", "job", build["id"], build["path"] + ["selftests"])
        self.before = [build, group]
        self.before += [
            node(12 + i, name, "test", group["id"], group["path"] + [name], result)
            for i, (name, result) in enumerate((("good", "pass"), ("existing", "fail")))
        ]
        mapping = {BASE_ID: ROOT_ID} | {
            n["id"]: f"{int(n['id'], 16)+100:024x}" for n in self.before
        }
        self.after = [
            deepcopy(n)
            | {
                "id": mapping[n["id"]],
                "parent": mapping[n["parent"]],
                "treeid": TREE,
                "path": ["checkout", "patchset"] + n["path"][1:],
            }
            for n in self.before
        ]
        self.client = Mock(spec=KernelCIClient)
        self.client.get_node.side_effect = lambda ident, **opts: deepcopy(
            self.base if ident == BASE_ID else self.root
        )
        self.client.get_nodes.side_effect = self.get_nodes
        load = patch(
            "kci_patchwork.results.load_run",
            side_effect=lambda _: (deepcopy(self.manifest), deepcopy(self.state), []),
        )
        load.start()
        self.addCleanup(load.stop)

    def get_nodes(self, **opts):
        self.assertEqual(opts["api_url"], API)
        if opts["filters"] == [f"treeid={TREE}"]:
            rows = [self.root] + self.after
        else:
            self.assertEqual(opts["filters"][0], f"treeid={BASE_TREE}")
            self.assertEqual(len(opts["filters"]), 2)
            self.assertTrue(opts["filters"][1].startswith("path="))
            job = opts["filters"][1].removeprefix("path=")
            self.assertIn(
                job, [n["name"] for n in self.after if n["parent"] == ROOT_ID]
            )
            rows = [n for n in self.before if job in n["path"]]
        return deepcopy(rows[opts["offset"] : opts["offset"] + opts["limit"]])

    def collect(self, **kwargs):
        return collect(self.client, "unused", **kwargs)

    def test_existing_failures_pass_comparison_and_preserve_raw_failures(self):
        report = self.collect(page_size=2)
        self.assertEqual(report["status"], "NO_REGRESSIONS_IN_OBSERVED_RESULTS")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["counts"]["results"]["fail"], 1)
        self.assertEqual(report["comparison"]["counts"]["existing_failure"], 1)
        self.assertEqual(report["comparison"]["counts"]["unchanged_pass"], 3)
        self.assertTrue(report["comparison"]["complete"])
        self.assertFalse(report["approved"])
        self.assertEqual(report["required_coverage"], "not_assessed")
        self.client.trigger_patchset.assert_not_called()
        self.client.compare_results.assert_not_called()
        for call in self.client.get_node.call_args_list:
            self.assertEqual(call.kwargs["api_url"], API)

    def test_new_regression_is_not_hidden_by_existing_failure(self):
        self.after[2]["result"] = "fail"
        report = self.collect()
        self.assertEqual(report["status"], "REGRESSIONS_DETECTED")
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["comparison"]["counts"]["regression"], 1)
        self.assertEqual(report["comparison"]["counts"]["existing_failure"], 1)

    def test_fixed_failure(self):
        self.after[3]["result"] = "pass"
        report = self.collect()
        self.assertEqual(report["status"], "NO_FAILURES_IN_OBSERVED_RESULTS")
        self.assertEqual(report["comparison"]["counts"]["fixed"], 1)

    def test_new_results_and_missing_results_do_not_pass(self):
        scenarios = (
            ("fail", "REVIEW_REQUIRED", "new_failure"),
            ("pass", "EVIDENCE_INCOMPLETE", "new_result"),
        )
        original = deepcopy(self.after)
        for result, status, category in scenarios:
            self.after = deepcopy(original) + [
                deepcopy(original[2])
                | {"id": "f" * 24, "name": "new", "result": result}
            ]
            with self.subTest(result=result):
                report = self.collect()
                self.assertEqual(report["status"], status)
                self.assertEqual(report["comparison"]["counts"][category], 1)
                self.assertFalse(report["comparison"]["complete"])
        self.after = original[:3]
        report = self.collect()
        self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
        self.assertEqual(report["comparison"]["counts"]["missing"], 1)

    def test_configuration_or_test_revision_mismatch_never_becomes_existing_failure(
        self,
    ):
        for field in (
            "arch",
            "compiler",
            "config_full",
            "platform",
            "device",
            "test_revision",
        ):
            original = deepcopy(self.after)
            self.after[0]["data"][field] = "different"
            with self.subTest(field=field):
                report = self.collect()
                self.assertNotEqual(report["exit_code"], 0)
                self.assertEqual(report["comparison"]["counts"]["existing_failure"], 0)
                self.assertFalse(report["comparison"]["complete"])
            self.after = original

    def test_duplicate_result_or_retry_ancestor_is_ambiguous(self):
        for side in ("before", "after"):
            original = deepcopy(getattr(self, side))
            for index in (0, 3):
                setattr(
                    self,
                    side,
                    deepcopy(original) + [deepcopy(original[index]) | {"id": "f" * 24}],
                )
                with self.subTest(side=side, index=index):
                    report = self.collect()
                    self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
                    self.assertGreater(report["comparison"]["counts"]["ambiguous"], 0)
                    self.assertEqual(
                        report["comparison"]["counts"]["existing_failure"], 0
                    )
            setattr(self, side, original)

    def test_unrelated_platform_variant_and_other_jobs_are_excluded(self):
        variant = deepcopy(self.before)
        ids = {n["id"]: f"{int(n['id'],16)+200:024x}" for n in variant}
        for n in variant:
            n["parent"] = ids.get(n["parent"], n["parent"])
            n["id"] = ids[n["id"]]
            n["result"] = "fail"
        variant[0]["data"]["platform"] = "another-platform"
        self.before += variant
        self.before += [
            node(1000 + i, "unrelated", "kbuild", BASE_ID, ["checkout", "other-job"])
            for i in range(200)
        ]
        report = self.collect(max_nodes=10)
        self.assertEqual(report["status"], "NO_REGRESSIONS_IN_OBSERVED_RESULTS")
        self.assertEqual(report["comparison"]["selected_baseline_nodes"], 4)
        self.assertEqual(len(report["comparison"]["nodes"]), 8)

    def test_same_named_tests_in_different_suites_remain_separate(self):
        for rows, offset in ((self.before, 0), (self.after, 100)):
            group = deepcopy(rows[1]) | {
                "id": f"{30+offset:024x}",
                "name": "other-suite",
            }
            test = deepcopy(rows[2]) | {
                "id": f"{31+offset:024x}",
                "parent": group["id"],
                "name": "existing",
            }
            rows.extend([group, test])
        self.after[3]["result"] = "pass"
        self.after[-1]["result"] = "fail"
        report = self.collect()
        self.assertEqual(report["comparison"]["counts"]["fixed"], 1)
        self.assertEqual(report["comparison"]["counts"]["regression"], 1)

    def test_baseline_identity_changes_are_rejected(self):
        original = deepcopy(self.base)
        for key, value in (
            ("treeid", "f" * 64),
            ("id", "f" * 24),
            ("name", "patchset"),
        ):
            self.base = deepcopy(original) | {key: value}
            with self.subTest(key=key):
                report = self.collect()
                self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
                self.assertTrue(report["comparison"]["errors"])

    def test_changed_revision_or_patchset_on_baseline_descendant_is_rejected(self):
        for revision in ({"commit": "d" * 40}, {"patchset": "e" * 64}):
            self.before[3]["data"]["kernel_revision"] = revision
            with self.subTest(revision=revision):
                report = self.collect()
                self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
                self.assertTrue(report["comparison"]["errors"])

    def test_baseline_read_failure_preserves_patched_evidence(self):
        def get(ident, **opts):
            if ident == BASE_ID:
                raise TimeoutError("baseline unavailable")
            return deepcopy(self.root)

        self.client.get_node.side_effect = get
        report = self.collect()
        self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
        self.assertEqual(report["counts"]["results"]["fail"], 1)
        self.assertTrue(report["comparison"]["errors"])

    def test_baseline_partial_pages_and_repeated_pages_do_not_pass(self):
        for repeat in (False, True):

            def get(**opts):
                if opts["filters"][0] == f"treeid={BASE_TREE}" and opts["offset"]:
                    if not repeat:
                        raise TimeoutError("lost baseline page")
                    opts["offset"] = 0
                return self.get_nodes(**opts)

            self.client.get_nodes.side_effect = get
            with self.subTest(repeat=repeat):
                report = self.collect(page_size=2)
                self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
                self.assertFalse(report["comparison"]["listing_complete"])
                self.assertEqual(len(report["comparison"]["nodes"]), 2)

    def test_baseline_limit_is_independent_and_combined_across_subtrees(self):
        other = node(20, "other-job", "kbuild", BASE_ID, ["checkout", "other-job"])
        self.before.append(other)
        self.after.append(
            deepcopy(other)
            | {
                "id": f"{120:024x}",
                "parent": ROOT_ID,
                "treeid": TREE,
                "path": ["checkout", "patchset", "other-job"],
            }
        )
        self.before += [
            node(
                500 + i,
                f"extra-{i}",
                "test",
                other["id"],
                other["path"] + [f"extra-{i}"],
            )
            for i in range(3)
        ]
        report = self.collect(max_nodes=7, page_size=2)
        self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
        self.assertTrue(report["listing_complete"])
        self.assertEqual(len(report["comparison"]["nodes"]), 7)
        self.assertEqual(report["comparison"]["job_paths"], [JOB, "other-job"])
        self.assertIn("node limit", report["comparison"]["errors"][0])

    def test_wrong_tree_and_broken_ancestry_are_rejected(self):
        original = deepcopy(self.before)
        for change in (
            {"treeid": TREE},
            {"parent": "f" * 24},
            {"parent": self.before[3]["id"]},
        ):
            self.before = deepcopy(original)
            self.before[3].update(change)
            with self.subTest(change=change):
                report = self.collect()
                self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
                self.assertTrue(report["comparison"]["errors"])

    def test_ignored_baseline_path_filter_is_rejected(self):
        def get(**opts):
            rows = self.get_nodes(**opts)
            if rows and opts["filters"][0] == f"treeid={BASE_TREE}":
                rows[0]["path"] = ["checkout", "unrelated-job"]
            return rows

        self.client.get_nodes.side_effect = get
        report = self.collect()
        self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
        self.assertIn("out-of-scope", report["comparison"]["errors"][0])

    def test_nonterminal_and_unknown_baseline_outcomes_do_not_pass(self):
        for change in (
            {"state": "available"},
            {"result": None},
            {"result": "incomplete"},
        ):
            original = deepcopy(self.before)
            self.before[3].update(change)
            with self.subTest(change=change):
                report = self.collect()
                self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
                self.assertEqual(report["comparison"]["counts"]["incomplete"], 1)
            self.before = original
        self.base["state"] = "closing"
        self.assertEqual(self.collect()["status"], "EVIDENCE_INCOMPLETE")

    def test_skipped_transitions_are_not_treated_as_successful_comparisons(self):
        cases = (
            ("skip", "fail", "new_failure"),
            ("fail", "skip", "incomparable"),
            ("skip", "pass", "incomparable"),
        )
        for old, new, category in cases:
            self.before[3]["result"], self.after[3]["result"] = old, new
            with self.subTest(old=old, new=new):
                report = self.collect()
                self.assertNotEqual(report["exit_code"], 0)
                self.assertEqual(report["comparison"]["counts"][category], 1)
        self.before[3]["result"] = self.after[3]["result"] = "skip"
        self.assertEqual(self.collect()["comparison"]["counts"]["unchanged_skip"], 1)

    def test_running_and_failed_patchsets_do_not_fetch_baseline(self):
        for changes, status in (
            ({"state": "available"}, "RUNNING"),
            ({"result": "fail"}, "PATCH_APPLICATION_FAILED"),
        ):
            original = deepcopy(self.root)
            self.root.update(changes)
            self.client.get_node.reset_mock()
            with self.subTest(status=status):
                report = self.collect()
                self.assertEqual(report["status"], status)
                self.assertFalse(report["comparison"]["performed"])
                self.assertTrue(
                    all(
                        call.args[0] == ROOT_ID
                        for call in self.client.get_node.call_args_list
                    )
                )
            self.root = original

    def test_report_includes_comparison_and_escapes_remote_names(self):
        for rows in (self.before, self.after):
            rows[3]["name"] = '<script>alert("bad")</script>'
        report = self.collect()
        html = render(report)
        self.assertIn("Baseline comparison", html)
        self.assertIn("existing failure", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        summary = report_summary(report, Path("run"))
        self.assertTrue(summary["comparison"]["complete"])
        self.assertNotIn("nodes", summary["comparison"])
        json.dumps(summary)


if __name__ == "__main__":
    unittest.main()

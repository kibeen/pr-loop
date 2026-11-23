"""Unit tests for the pr-merge-loop watcher's decision logic (no network, no gh).

  python -m unittest discover -s tests

Ported from tests/pr-merge-watch.test.mjs when the watcher dropped Node.js. The command
tests used to be skipped on Windows, because the fake `gh` was a shebang script; here they
intercept the module's own subprocess call instead, so they run on every platform.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "pr-merge-loop" / "scripts" / "pr_merge_watch.py"


def _load():
    spec = importlib.util.spec_from_file_location("pr_merge_watch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


w = _load()

if sys.version_info[:2] < w.MIN_PYTHON:
    # Below the floor, every test that calls main() would hand over to a newer
    # interpreter, and on POSIX that exec replaces the test runner itself. CI runs
    # the floor and above; stop here instead.
    raise RuntimeError(
        "run this suite on Python %s or newer, not %s" % (w.floor_str(), w.version_str())
    )

ME = "author"


def user(login):
    return {"__typename": "User", "login": login}


def check_run(name, status, conclusion, **o):
    return {
        "__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
        "detailsUrl": "https://ci/%s" % name,
        "startedAt": o.get("startedAt", "2026-01-01T00:00:00Z"),
        "isRequired": bool(o.get("required")),
        "checkSuite": {"workflowRun": {
            "databaseId": o.get("runId", 1), "event": o.get("event", "pull_request"),
            "workflow": {"name": o.get("workflow", "CI")},
        }},
    }


def ok(name, **o):
    return check_run(name, "COMPLETED", "SUCCESS", **o)


def bad(name, **o):
    return check_run(name, "COMPLETED", "FAILURE", **o)


def running(name, **o):
    return check_run(name, "IN_PROGRESS", None, **o)


def payload(over=None, repo=None, contexts=None):
    """A GraphQL payload for an open, non-draft PR; `over` patches the pullRequest, `repo` the repository."""
    pull = {
        "number": 412, "url": "https://github.com/acme/engine/pull/412", "title": "t",
        "state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED",
        "headRefOid": "a1b2c3d", "headRefName": "feat", "baseRefName": "main",
        "isInMergeQueue": False, "mergedAt": None, "mergeCommit": None,
        "author": {"login": ME}, "autoMergeRequest": None,
        "baseRef": {"branchProtectionRule": None},
        "reviewRequests": {"nodes": []}, "latestOpinionatedReviews": {"nodes": []},
        "latestReviews": {"nodes": []}, "reviews": {"nodes": []},
        "reviewThreads": {"nodes": []}, "comments": {"nodes": []},
        "commits": {"nodes": [{"commit": {"oid": "a1b2c3d", "statusCheckRollup": {
            "state": "PENDING",
            "contexts": {"pageInfo": {"hasNextPage": False}, "nodes": list(contexts or [])},
        }}}]},
    }
    pull.update(over or {})
    repository = {
        "nameWithOwner": "acme/engine", "autoMergeAllowed": True, "mergeCommitAllowed": True,
        "squashMergeAllowed": True, "rebaseMergeAllowed": False,
        "viewerDefaultMergeMethod": "SQUASH",
    }
    repository.update(repo or {})
    repository["pullRequest"] = pull
    return {"viewer": {"login": ME}, "repository": repository}


def snap(over=None, repo=None, contexts=None, rulesets=None):
    return w.build_snapshot(payload(over, repo, contexts), rulesets or [])


def approved_by(login, oid="a1b2c3d"):
    """A person's approval of the given commit (the payload's head by default)."""
    return {"latestOpinionatedReviews": {"nodes": [
        {"author": user(login), "state": "APPROVED", "commit": {"oid": oid}}
    ]}}


def mark(url, text, at):
    return {
        "url": url, "body": "<!-- pr-review-loop %s -->\nnote" % text,
        "createdAt": at, "author": user(ME),
    }


AUTO = {"autoMergeRequest": {
    "enabledAt": "2026-01-01T00:00:00Z", "mergeMethod": "SQUASH", "enabledBy": {"login": ME},
}}


def pull_of(over=None, repo=None, contexts=None):
    return payload(over, repo, contexts)["repository"]["pullRequest"]


def with_event(s, event):
    out = dict(s)
    out["event"] = event
    return out


class MarkerTest(unittest.TestCase):
    def test_parse_marker_reads_step_round_sha_until_and_ask(self):
        m = w.parse_marker(
            "<!-- pr-review-loop step=feedback round=1 sha=abc "
            "until=2026-11-13T09:00:00Z ask=PRRT_kwDO-a_1,IC_kwDOb -->"
        )
        self.assertEqual(m["ask"], "PRRT_kwDO-a_1,IC_kwDOb")
        self.assertEqual(
            w.parse_marker(
                "<!-- pr-review-loop step=feedback round=2 sha=abc until=2026-11-13T09:00:00Z -->\nbody"
            ),
            {"step": "feedback", "round": "2", "sha": "abc", "until": "2026-11-13T09:00:00Z"},
        )
        self.assertIsNone(w.parse_marker("plain comment"))
        self.assertIsNone(w.parse_marker("<!-- pr-review-loop -->"))

    def test_ts_reads_offsets_and_rejects_junk(self):
        """Date.parse's stand-in: the watcher compares these numbers to decide what is settled."""
        self.assertEqual(w.ts("2026-01-01T00:00:00Z"), 1767225600000)
        self.assertEqual(w.ts("2026-01-01T09:00:00+09:00"), w.ts("2026-01-01T00:00:00Z"))
        self.assertEqual(w.ts("2026-01-01T00:00:00.500Z"), w.ts("2026-01-01T00:00:00Z") + 500)
        for junk in (None, "", "yesterday", "2026-13-01T00:00:00Z"):
            self.assertEqual(w.ts(junk), 0, junk)


class TerminalStateTest(unittest.TestCase):
    def test_terminal_and_draft_states_win_over_everything_else(self):
        merged = snap(
            {"state": "MERGED", "mergedAt": "2026-01-02T00:00:00Z", "mergeCommit": {"oid": "f" * 40}},
            {}, [bad("Unit", required=True)],
        )
        self.assertEqual(w.classify(merged), ["merged"])
        self.assertEqual(w.classify(snap({"state": "CLOSED"})), ["closed"])
        self.assertEqual(w.classify(snap({"isDraft": True, "mergeStateStatus": "DRAFT"})), ["draft"])


class ChecksTest(unittest.TestCase):
    def test_a_failed_required_check_is_ci_failed_and_carries_the_run_id(self):
        s = snap({}, {}, [
            bad("Unit Tests", required=True, runId=77), ok("lint", required=True), bad("nightly-perf"),
        ])
        self.assertEqual(w.classify(s), ["ci_failed"])
        self.assertEqual([[c["name"], c["runId"]] for c in s["checks"]["failed"]], [["Unit Tests", 77]])
        self.assertEqual([c["name"] for c in s["checks"]["optionalFailed"]], ["nightly-perf"])
        self.assertEqual(s["checks"]["gating"], "required")

    def test_an_optional_failure_does_not_wake_the_agent_while_required_checks_exist(self):
        s = snap({}, {}, [ok("Unit Tests", required=True), bad("nightly-perf")])
        self.assertEqual(w.classify(s), [])

    def test_with_no_required_checks_every_check_gates_the_merge(self):
        s = snap({"reviewDecision": None}, {}, [ok("lint"), bad("build")])
        self.assertEqual(s["checks"]["gating"], "all")
        self.assertEqual(w.classify(s), ["ci_failed"])

    def test_a_rerun_replaces_the_earlier_failed_run_of_the_same_check(self):
        checks = w.normalize_checks([
            bad("Unit Tests", required=True, startedAt="2026-01-01T00:00:00Z", runId=1),
            running("Unit Tests", required=True, startedAt="2026-01-01T01:00:00Z", runId=2),
        ])
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["state"], "pending")
        s = w.summarize_checks(checks)
        self.assertEqual([len(s["failed"]), s["pending"]], [0, ["Unit Tests"]])

    def test_skipped_and_neutral_required_checks_count_as_passed_cancelled_counts_as_failed(self):
        s = w.summarize_checks(w.normalize_checks([
            check_run("a", "COMPLETED", "SKIPPED", required=True),
            check_run("b", "COMPLETED", "NEUTRAL", required=True),
            check_run("c", "COMPLETED", "CANCELLED", required=True),
            {"__typename": "StatusContext", "context": "legacy", "state": "ERROR",
             "targetUrl": None, "createdAt": "2026-01-01T00:00:00Z", "isRequired": True},
        ]))
        self.assertEqual(s["passed"], 2)
        self.assertEqual([c["name"] for c in s["failed"]], ["c", "legacy"])

    def test_a_required_check_that_was_skipped_is_not_ci_that_ran(self):
        approved = {"reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN"}
        skipped_run = [
            check_run("Unit Tests", "COMPLETED", "SKIPPED", required=True),
            ok("gate", required=True),
            check_run("docs", "COMPLETED", "SKIPPED"),
        ]
        s = snap(dict(AUTO, **approved), {}, skipped_run)
        self.assertEqual(s["checks"]["skipped"], ["Unit Tests"])  # the optional skipped job is not listed
        # GitHub calls this mergeable; we do not call it ready
        self.assertEqual(w.classify(s, no_auto=True), [])
        since = {}
        self.assertEqual(w.timed_events(s, {}, since, 0, 600_000), [])
        self.assertEqual(w.timed_events(s, {}, since, 600_000, 600_000), ["ci_not_started"])
        self.assertRegex(w.hint(with_event(s, "ci_not_started")), "Unit Tests")
        # before auto-merge is on, the effect says so instead of "merges_now"
        self.assertEqual(snap(approved, {}, skipped_run)["autoMerge"]["effect"], "ci_not_run")

        # a repository that skips this job on purpose says so with --skip-ok, for all checks or by name
        def with_skip(skip_ok):
            return w.build_snapshot(
                payload(dict(approved, **approved_by("bob")), {}, skipped_run), [], [], skip_ok=skip_ok
            )

        self.assertEqual(with_skip(True)["checks"]["skipped"], [])
        self.assertEqual(w.classify(with_skip(True), no_auto=True), ["ready"])
        self.assertEqual(with_skip(["Unit Tests"])["checks"]["skipped"], [])
        self.assertEqual(with_skip(["other"])["checks"]["skipped"], ["Unit Tests"])

    def test_the_latest_run_that_decided_something_speaks_for_a_check(self):
        def at(h):
            return "2026-01-01T0%d:00:00Z" % h

        def state(runs):
            return w.summarize_checks(w.normalize_checks(runs))

        # passed, then a label event added a skipped run: the check ran and passed
        s = state([
            ok("gate", required=True, startedAt=at(1)),
            check_run("gate", "COMPLETED", "SKIPPED", required=True, startedAt=at(2)),
        ])
        self.assertEqual([s["passed"], len(s["failed"]), s["skipped"]], [1, 0, []])
        # failed, then skipped: the failure is not hidden, with or without --skip-ok
        failed_then_skipped = [
            bad("Unit", required=True, startedAt=at(1), runId=5),
            check_run("Unit", "COMPLETED", "SKIPPED", required=True, startedAt=at(2)),
        ]
        self.assertEqual([c["runId"] for c in state(failed_then_skipped)["failed"]], [5])
        self.assertEqual(
            [c["runId"] for c in w.summarize_checks(
                w.normalize_checks(failed_then_skipped), [], skip_ok=True)["failed"]],
            [5],
        )
        # passed, then a superseded run was cancelled and its replacement skipped
        s = state([
            ok("gate", required=True, startedAt=at(1)),
            check_run("gate", "COMPLETED", "CANCELLED", required=True, startedAt=at(2)),
            check_run("gate", "COMPLETED", "SKIPPED", required=True, startedAt=at(3)),
        ])
        self.assertEqual([s["passed"], len(s["failed"]), s["skipped"]], [1, 0, []])
        # a new run after a pass is what counts while it is running, and when it fails
        self.assertEqual(
            state([ok("Unit", required=True, startedAt=at(1)),
                   running("Unit", required=True, startedAt=at(2))])["pending"],
            ["Unit"],
        )
        self.assertEqual(
            len(state([ok("Unit", required=True, startedAt=at(1)),
                       bad("Unit", required=True, startedAt=at(2))])["failed"]),
            1,
        )
        # only cancelled: needs a re-run. only skipped: never ran
        self.assertEqual(len(state([check_run("Unit", "COMPLETED", "CANCELLED", required=True)])["failed"]), 1)
        self.assertEqual(state([check_run("Unit", "COMPLETED", "SKIPPED", required=True)])["skipped"], ["Unit"])
        # skipped on pull_request but run by hand (workflow_dispatch) under the same name: it ran
        s = state([
            check_run("Unit", "COMPLETED", "SKIPPED", required=True),
            ok("Unit", required=True, event="workflow_dispatch"),
        ])
        self.assertEqual([s["skipped"], len(s["failed"])], [[], 0])

    def test_required_contexts_from_rulesets_that_never_reported_are_listed(self):
        rulesets = [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "Unit Tests"}, {"context": "gate"}]
        }}]
        s = snap(AUTO, {}, [ok("gate", required=True)], rulesets)
        self.assertEqual(s["checks"]["missingRequired"], ["Unit Tests"])
        self.assertEqual(w.classify(s, expect_auto=True), [])
        since = {}
        self.assertEqual(w.timed_events(s, {}, since, 0, 600_000), [])
        self.assertEqual(w.timed_events(s, {}, since, 599_000, 600_000), [])
        self.assertEqual(w.timed_events(s, {}, since, 600_000, 600_000), ["ci_not_started"])


class RulesTest(unittest.TestCase):
    def test_merge_rules_merges_classic_protection_and_rulesets(self):
        r = w.merge_rules(
            {"requiresStatusChecks": True, "requiredStatusCheckContexts": ["a"],
             "requiredApprovingReviewCount": 1, "dismissesStaleReviews": False,
             "requiresConversationResolution": True, "requiresStrictStatusChecks": False},
            [
                {"type": "required_status_checks", "parameters": {
                    "required_status_checks": [{"context": "a"}, {"context": "b"}],
                    "strict_required_status_checks_policy": True}},
                {"type": "pull_request", "parameters": {
                    "required_approving_review_count": 2, "dismiss_stale_reviews_on_push": True,
                    "allowed_merge_methods": ["squash", "rebase"]}},
                {"type": "pull_request", "parameters": {"allowed_merge_methods": ["merge", "squash"]}},
                {"type": "merge_queue", "parameters": {}},
                {"type": "deletion", "parameters": None},
            ],
        )
        self.assertEqual(r, {
            "requiredContexts": ["a", "b"], "requiredApprovals": 2, "dismissStaleOnPush": True,
            "threadResolutionRequired": True, "upToDateRequired": True, "mergeQueue": True,
            "allowedMergeMethods": ["squash"],
        })
        self.assertIsNone(w.merge_rules(None, [])["allowedMergeMethods"])

    def test_a_ruleset_narrows_what_the_repository_allows_and_the_default_follows(self):
        rulesets = [{"type": "pull_request", "parameters": {
            "required_approving_review_count": 1, "allowed_merge_methods": ["squash"]
        }}]
        s = snap({}, {"viewerDefaultMergeMethod": "MERGE"}, [], rulesets)
        self.assertEqual([s["repo"]["mergeMethods"], s["repo"]["defaultMergeMethod"]], [["squash"], "squash"])

    def test_rules_that_leave_no_method_allowed_offer_none(self):
        """JS read `[]` as truthy, so an empty intersection excluded every method.

        Reading it as "no restriction" would hand the skill a merge method the
        branch rules forbid, and SKILL.md picks the method from defaultMergeMethod.
        """
        disjoint = [
            {"type": "pull_request", "parameters": {"allowed_merge_methods": ["squash"]}},
            {"type": "pull_request", "parameters": {"allowed_merge_methods": ["rebase"]}},
        ]
        self.assertEqual(w.merge_rules(None, disjoint)["allowedMergeMethods"], [])
        s = snap({}, {}, [], disjoint)
        self.assertEqual(s["repo"]["mergeMethods"], [])
        self.assertIsNone(s["repo"]["defaultMergeMethod"])


class FeedbackTest(unittest.TestCase):
    def test_reviewer_feedback_is_reported_and_own_and_bot_noise_is_not(self):
        s = snap({
            "reviewDecision": "CHANGES_REQUESTED",
            "latestOpinionatedReviews": {"nodes": [
                {"author": {"login": "jane"}, "state": "CHANGES_REQUESTED",
                 "submittedAt": "2026-01-02T00:00:00Z"}
            ]},
            "reviews": {"nodes": [
                {"url": "r1", "state": "CHANGES_REQUESTED", "body": "",
                 "submittedAt": "2026-01-02T00:00:00Z", "author": user("jane")},
                # carrier of inline comments
                {"url": "r2", "state": "COMMENTED", "body": "",
                 "submittedAt": "2026-01-02T00:00:00Z", "author": user("jane")},
                # our own thread reply
                {"url": "r3", "state": "COMMENTED", "body": "",
                 "submittedAt": "2026-01-02T00:05:00Z", "author": user(ME)},
            ]},
            "reviewThreads": {"nodes": [
                {"id": "T1", "isResolved": False, "isOutdated": False, "path": "a.py", "line": 3,
                 "comments": {"nodes": [
                     {"url": "c1", "body": "rename tmp", "createdAt": "2026-01-02T00:00:00Z",
                      "author": user("jane")}]}},
                {"id": "T2", "isResolved": False, "isOutdated": False, "path": "a.py", "line": 9,
                 "comments": {"nodes": [
                     {"url": "c2", "body": "why the lock?", "createdAt": "2026-01-02T00:00:00Z",
                      "author": user("jane")},
                     {"url": "c3", "body": "it guards X", "createdAt": "2026-01-02T00:05:00Z",
                      "author": user(ME)}]}},
                {"id": "T3", "isResolved": True, "isOutdated": False, "path": "b.py", "line": 1,
                 "comments": {"nodes": [
                     {"url": "c4", "body": "done?", "createdAt": "2026-01-02T00:00:00Z",
                      "author": user("jane")}]}},
            ]},
            "comments": {"nodes": [
                {"url": "i1", "body": "<!-- pr-review-loop step=review round=1 sha=a1b2c3d -->\n## 리뷰",
                 "createdAt": "2026-01-01T00:00:00Z", "author": user(ME)},
                {"url": "i2", "body": "coverage 81%", "createdAt": "2026-01-02T00:00:00Z",
                 "author": {"__typename": "Bot", "login": "codecov"}},
                {"url": "i3", "body": "please also update the docs",
                 "createdAt": "2026-01-02T00:10:00Z", "author": user("bob")},
            ]},
        })
        self.assertEqual(
            [[i["kind"], i["author"], i.get("threadId") or i["url"]] for i in s["feedback"]["items"]],
            [["review", "jane", "r1"], ["thread", "jane", "T1"], ["comment", "bob", "i3"]],
        )
        self.assertEqual(s["markers"]["next"]["feedback"], 1)
        self.assertEqual(s["feedback"]["until"], "2026-01-02T00:10:00Z")
        self.assertEqual(s["review"]["unresolvedThreads"], 2)
        self.assertEqual(w.classify(s), ["feedback"])
        self.assertRegex(w.hint(with_event(s, "feedback")), "3건")

    def test_a_feedback_marker_settles_what_came_before_its_until(self):
        base = {
            "reviews": {"nodes": [
                {"url": "r1", "state": "CHANGES_REQUESTED", "body": "fix it",
                 "submittedAt": "2026-01-02T00:00:00Z", "author": user("jane")}
            ]},
            "comments": {"nodes": [
                {"url": "i3", "body": "docs please", "createdAt": "2026-01-02T00:10:00Z",
                 "author": user("bob")},
                {"url": "i4",
                 "body": "<!-- pr-review-loop step=feedback round=1 sha=bbb "
                         "until=2026-01-02T00:10:00Z -->\n- 고침",
                 "createdAt": "2026-01-02T01:00:00Z", "author": user(ME)},
            ]},
        }
        fb = w.collect_feedback(pull_of(base), ME)
        self.assertEqual(
            [len(fb["items"]), w.summarize_markers(fb["markers"])["next"]["feedback"], fb["until"]],
            [0, 2, None],
        )
        base["comments"]["nodes"].append({
            "url": "i5", "body": "one more thing", "createdAt": "2026-01-02T00:10:01Z",
            "author": user("bob"),
        })
        fb = w.collect_feedback(pull_of(base), ME)
        # written before our reply was posted, after the snapshot we answered
        self.assertEqual([i["url"] for i in fb["items"]], ["i5"])

    def test_what_a_reviewer_wrote_before_their_own_approval_is_settled(self):
        fb = w.collect_feedback(pull_of({
            "reviews": {"nodes": [
                {"url": "r1", "state": "CHANGES_REQUESTED", "body": "fix it",
                 "submittedAt": "2026-01-02T00:00:00Z", "author": user("jane")},
                {"url": "r2", "state": "APPROVED", "body": "",
                 "submittedAt": "2026-01-03T00:00:00Z", "author": user("jane")},
            ]},
            "reviewThreads": {"nodes": [
                {"id": "T1", "isResolved": False, "isOutdated": True, "path": "a.py", "line": None,
                 "comments": {"nodes": [
                     {"url": "c1", "body": "nit", "createdAt": "2026-01-02T00:00:00Z",
                      "author": user("jane")}]}}
            ]},
        }), ME)
        self.assertEqual(len(fb["items"]), 0)

    def test_nits_submitted_together_with_an_approval_stay_open(self):
        fb = w.collect_feedback(pull_of({
            "reviews": {"nodes": [
                {"url": "r1", "state": "APPROVED", "body": "",
                 "submittedAt": "2026-01-03T09:50:45Z", "author": user("jane")}
            ]},
            "reviewThreads": {"nodes": [
                {"id": "T1", "isResolved": False, "isOutdated": False, "path": "a.py", "line": 7,
                 "comments": {"nodes": [
                     {"url": "c1", "body": "nit: rename", "createdAt": "2026-01-03T09:48:00Z",
                      "publishedAt": "2026-01-03T09:50:45Z", "author": user("jane")}]}}
            ]},
        }), ME)
        self.assertEqual([[i["kind"], i["threadId"]] for i in fb["items"]], [["thread", "T1"]])


def parked_pr(ask, t1=None, comments=None, pr=None):
    """A declined item waits for the user's approval of the reply: the feedback marker parks it in `ask`."""
    out = {
        "reviewDecision": "CHANGES_REQUESTED",
        "reviews": {"nodes": [
            {"id": "R1", "url": "r1", "state": "CHANGES_REQUESTED", "body": "drop the cache",
             "submittedAt": "2026-01-02T00:00:00Z", "author": user("jane")}
        ]},
        "reviewThreads": {"nodes": [
            {"id": "T1", "isResolved": False, "isOutdated": False, "path": "a.py", "line": 3,
             "comments": {"nodes": [
                 {"url": "c1", "body": "use a dict here", "createdAt": "2026-01-02T00:00:00Z",
                  "author": user("jane")},
             ] + list(t1 or [])}},
            {"id": "T2", "isResolved": False, "isOutdated": False, "path": "a.py", "line": 9,
             "comments": {"nodes": [
                 {"url": "c2", "body": "rename tmp", "createdAt": "2026-01-02T00:00:00Z",
                  "author": user("jane")}]}},
        ]},
        "comments": {"nodes": [
            {"id": "C1", "url": "i1", "body": "split this PR",
             "createdAt": "2026-01-02T00:10:00Z", "author": user("bob")},
            mark("i2", "step=feedback round=1 sha=bbb until=2026-01-02T00:10:00Z"
                 + (" ask=%s" % ask if ask else ""), "2026-01-02T01:00:00Z"),
        ] + list(comments or [])},
    }
    out.update(pr or {})
    return out


class AskTest(unittest.TestCase):
    def test_parked_items_do_not_wake_the_agent_and_are_listed_for_the_user(self):
        s = snap(parked_pr("T1,R1,C1"))
        self.assertEqual(
            [[i["kind"], i["id"]] for i in s["feedback"]["awaitingUser"]],
            [["review", "R1"], ["thread", "T1"], ["comment", "C1"]],
        )
        self.assertEqual([i["id"] for i in s["feedback"]["items"]], ["T2"])
        self.assertEqual(w.classify(s), ["feedback"])
        self.assertEqual(s["feedback"]["awaitingUser"][1]["threadId"], "T1")
        self.assertEqual(s["feedback"]["awaitingUser"][1]["comments"][0]["body"], "use a dict here")

    def test_with_every_open_item_parked_the_watcher_waits_and_says_what_for(self):
        s = snap(parked_pr("T1,T2"))
        self.assertEqual(w.classify(s), [])
        self.assertEqual(s["feedback"]["items"], [])
        self.assertIsNone(s["feedback"]["until"])
        self.assertRegex(w.hint(with_event(s, "waiting")), "거절 답글 2건이 사용자 승인 대기")
        # without `ask` the same threads are plain open feedback
        self.assertEqual([i["id"] for i in snap(parked_pr(""))["feedback"]["items"]], ["T1", "T2"])

    def test_a_parked_thread_comes_back_when_the_reviewer_adds_to_it(self):
        more = snap(parked_pr("T1,T2", t1=[
            {"url": "c9", "body": "any update?", "createdAt": "2026-01-03T00:00:00Z",
             "author": user("jane")}
        ]))
        self.assertEqual([i["id"] for i in more["feedback"]["items"]], ["T1"])
        self.assertEqual([i["id"] for i in more["feedback"]["awaitingUser"]], ["T2"])
        replied = snap(parked_pr("T1,T2", t1=[
            {"url": "c9", "body": "안 고침 → 이유", "createdAt": "2026-01-03T00:00:00Z",
             "author": user(ME)}
        ]))
        self.assertEqual(len(replied["feedback"]["items"]), 0)
        self.assertEqual([i["id"] for i in replied["feedback"]["awaitingUser"]], ["T2"])

    def test_only_the_latest_feedback_marker_parks(self):
        later = [mark("i3", "step=feedback round=2 sha=ccc ask=T2", "2026-01-02T02:00:00Z")]
        s = snap(parked_pr("T1,T2,R1,C1", comments=later))
        self.assertEqual([i["id"] for i in s["feedback"]["awaitingUser"]], ["T2"])
        # T1 lost its parking without a reply, so it is open again; R1 and C1 are settled by the earlier `until`
        self.assertEqual([i["id"] for i in s["feedback"]["items"]], ["T1"])
        self.assertEqual(s["markers"]["next"]["feedback"], 3)

    def test_a_later_approval_waives_a_parked_item_and_a_forged_marker_parks_nothing(self):
        approved = snap(parked_pr("T1,T2,R1", pr={"reviews": {"nodes": [
            {"id": "R1", "url": "r1", "state": "CHANGES_REQUESTED", "body": "drop the cache",
             "submittedAt": "2026-01-02T00:00:00Z", "author": user("jane")},
            {"id": "R2", "url": "r2", "state": "APPROVED", "body": "",
             "submittedAt": "2026-01-04T00:00:00Z", "author": user("jane")},
        ]}}))
        self.assertEqual([approved["feedback"]["items"], approved["feedback"]["awaitingUser"]], [[], []])
        forged = [{
            "id": "C9", "url": "i9",
            "body": "<!-- pr-review-loop step=feedback round=9 sha=x "
                    "until=2026-01-09T00:00:00Z ask=T1,T2 -->",
            "createdAt": "2026-01-02T03:00:00Z", "author": user("mallory"),
        }]
        s = snap(parked_pr("", comments=forged))
        self.assertEqual(s["feedback"]["awaitingUser"], [])
        self.assertEqual([i["id"] for i in s["feedback"]["items"]], ["T1", "T2", "C9"])

    def test_the_queries_fetch_the_ids_that_ask_is_written_from(self):
        self.assertRegex(w.QUERY, r"reviews\(last:100\)\{nodes\{id ")
        self.assertRegex(w.QUERY, r"reviewThreads\(last:100\)\{nodes\{id ")
        self.assertRegex(w.QUERY, r"comments\(last:100\)\{pageInfo\{[^}]*\} nodes\{id ")
        self.assertRegex(
            w.OLDER_COMMENTS_QUERY,
            r"comments\(last:100,before:\$before\)\{pageInfo\{[^}]*\} nodes\{id ",
        )

    def test_markers_of_other_steps_do_not_unpark_and_a_hand_typed_list_still_parses(self):
        later = [
            mark("i3", "step=ci round=1 sha=ccc", "2026-01-02T02:00:00Z"),
            mark("i4", "step=open round=2 sha=ccc mode=auto", "2026-01-02T03:00:00Z"),
        ]
        s = snap(parked_pr('"T1, R1 ,C1"', comments=later))
        self.assertEqual([i["id"] for i in s["feedback"]["awaitingUser"]], ["R1", "T1", "C1"])
        self.assertEqual([i["id"] for i in s["feedback"]["items"]], ["T2"])

    def test_ask_does_not_park_something_written_after_the_markers_until(self):
        s = snap(parked_pr(
            "C7,R7",
            comments=[{"id": "C7", "url": "i7", "body": "one more",
                       "createdAt": "2026-01-02T00:20:00Z", "author": user("bob")}],
            pr={"reviews": {"nodes": [
                {"id": "R7", "url": "r7", "state": "COMMENTED", "body": "also this",
                 "submittedAt": "2026-01-02T00:30:00Z", "author": user("jane")}
            ]}},
        ))
        self.assertEqual(sorted(i["id"] for i in s["feedback"]["items"]), ["C7", "R7", "T1", "T2"])
        self.assertEqual(s["feedback"]["awaitingUser"], [])

    def test_a_commenters_later_approval_waives_their_parked_comment(self):
        s = snap(parked_pr("C1,T1,T2", pr={"reviews": {"nodes": [
            {"id": "R3", "url": "r3", "state": "APPROVED", "body": "",
             "submittedAt": "2026-01-04T00:00:00Z", "author": user("bob")}
        ]}}))
        self.assertEqual([i["id"] for i in s["feedback"]["awaitingUser"]], ["T1", "T2"])

    def test_a_parked_thread_that_must_be_resolved_is_a_wait_not_a_stuck_merge(self):
        rules = [{"type": "pull_request", "parameters": {
            "required_approving_review_count": 1, "required_review_thread_resolution": True
        }}]
        pr = dict(parked_pr("T1,T2"), reviewDecision="APPROVED", **approved_by("zoe"))
        pr.update(AUTO)
        green = [ok("Unit", required=True)]
        s = w.build_snapshot(payload(pr, {}, green), rules)
        self.assertTrue(s["rules"]["threadResolutionRequired"])
        self.assertEqual(w.timed_events(s, {}, {}, 0, 0), [])
        open_threads = w.build_snapshot(
            payload(dict(pr, reviewThreads={"nodes": []}), {}, green), rules
        )
        self.assertEqual(w.timed_events(open_threads, {}, {}, 0, 0), ["stuck"])
        # a parked thread explains nothing when threads need not be resolved
        loose = w.build_snapshot(
            payload(pr, {}, green),
            [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}],
        )
        self.assertEqual(len(loose["feedback"]["awaitingUser"]), 2)
        self.assertEqual(w.timed_events(loose, {}, {}, 0, 0), ["stuck"])
        # and a parked comment never does
        only_comment = dict(parked_pr("C1"), reviewThreads={"nodes": []},
                            reviewDecision="APPROVED", **approved_by("zoe"))
        only_comment.update(AUTO)
        comment = w.build_snapshot(payload(only_comment, {}, green), rules)
        self.assertEqual([i["id"] for i in comment["feedback"]["awaitingUser"]], ["C1"])
        self.assertEqual(w.timed_events(comment, {}, {}, 0, 0), ["stuck"])

    def test_a_comma_before_the_next_key_or_quotes_do_not_corrupt_the_marker(self):
        m = w.parse_marker(
            "<!-- pr-review-loop step=feedback round=1 sha=abc ask=T1,R1, "
            "until=2026-01-02T00:10:00Z -->"
        )
        self.assertEqual(m["until"], "2026-01-02T00:10:00Z")
        s = snap(dict(parked_pr(""), comments={"nodes": [
            {"id": "C1", "url": "i1", "body": "split this PR",
             "createdAt": "2026-01-02T00:10:00Z", "author": user("bob")},
            mark("i2", "step=feedback round=1 sha=bbb ask='T1', 'R1', "
                 "until=2026-01-02T00:10:00Z", "2026-01-02T01:00:00Z"),
        ]}))
        self.assertEqual([i["id"] for i in s["feedback"]["awaitingUser"]], ["R1", "T1"])
        self.assertEqual([i["id"] for i in s["feedback"]["items"]], ["T2"])

    def test_a_merged_pr_reminds_that_declined_replies_were_still_waiting(self):
        s = snap(parked_pr("T1", pr={
            "state": "MERGED", "mergedAt": "2026-01-05T00:00:00Z", "mergeCommit": {"oid": "f" * 40},
        }))
        self.assertEqual(w.classify(s), ["merged"])
        self.assertRegex(w.hint(with_event(s, "merged")), "거절 답글 1건")
        closed = snap(parked_pr("T1", pr={"state": "CLOSED"}))
        self.assertRegex(w.hint(with_event(closed, "closed")), "거절 답글 1건")
        self.assertNotRegex(
            w.hint(with_event(snap({"state": "CLOSED"}), "closed")), "거절 답글"
        )


class AutoMergeTest(unittest.TestCase):
    def test_conflict_behind_and_a_switched_off_auto_merge_are_reported_in_priority_order(self):
        s = snap({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}, {},
                 [bad("Unit", required=True)])
        self.assertEqual(w.classify(s, expect_auto=True), ["conflict", "ci_failed", "auto_merge_off"])
        self.assertEqual(
            w.classify(snap(dict(AUTO, mergeStateStatus="BEHIND")), expect_auto=True), ["behind"]
        )
        self.assertEqual(w.classify(snap(AUTO), expect_auto=True), [])

    def test_auto_merge_effect_tells_arming_apart_from_an_immediate_merge(self):
        def eff(over=None, repo=None, ctx=None, rules=None):
            return w.auto_merge_effect(snap(over, repo, ctx, rules))

        self.assertEqual(eff({"isDraft": True, "mergeStateStatus": "DRAFT"}), "draft")
        self.assertEqual(eff({}, {"autoMergeAllowed": False}), "not_allowed")
        self.assertEqual(eff({}), "arms")  # blocked on review
        self.assertEqual(eff(AUTO), "enabled")
        self.assertEqual(eff({"mergeStateStatus": "UNKNOWN"}), "unknown")
        # Nothing requires a review and nobody approved: the merge would be unreviewed.
        self.assertEqual(
            eff({"mergeStateStatus": "CLEAN", "reviewDecision": None}, {}, [ok("lint")]), "no_review"
        )
        self.assertEqual(
            eff({"mergeStateStatus": "UNSTABLE", "reviewDecision": None}, {}, [bad("lint")]), "no_review"
        )
        self.assertEqual(
            eff({"mergeStateStatus": "CLEAN", "reviewDecision": None}, {},
                [ok("Unit", required=True)]), "no_review"
        )
        self.assertEqual(
            eff({"mergeStateStatus": "BLOCKED", "reviewDecision": None}, {},
                [running("Unit", required=True)]), "no_review"
        )
        self.assertRegex(
            snap({"mergeStateStatus": "BLOCKED", "reviewDecision": None}, {},
                 [running("Unit", required=True)])["autoMerge"]["note"],
            "필수 체크만 통과하면 리뷰 없이",
        )
        self.assertRegex(
            snap({"mergeStateStatus": "UNSTABLE", "reviewDecision": None}, {},
                 [bad("lint")])["autoMerge"]["note"],
            "실패한 체크: lint",
        )
        # A person approved this very commit and everything is green: merging now is fine...
        self.assertEqual(
            eff(dict({"mergeStateStatus": "CLEAN", "reviewDecision": None}, **approved_by("bob")),
                {}, [ok("Unit", required=True)]), "merges_now"
        )
        # ...but with checks still running, an armed auto-merge would not wait for a new approval
        self.assertEqual(
            eff(dict({"mergeStateStatus": "BLOCKED", "reviewDecision": None}, **approved_by("bob")),
                {}, [running("Unit", required=True)]), "not_enforced"
        )
        self.assertEqual(
            eff(dict({"mergeStateStatus": "CLEAN", "reviewDecision": None},
                     **approved_by("bob", "0ld")), {}, [ok("Unit", required=True)]), "no_review"
        )
        # Rules exist and are already satisfied: merging now is what the rules allow.
        self.assertEqual(
            eff({"mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED"}, {},
                [ok("Unit", required=True)]), "merges_now"
        )
        # GitHub says CLEAN because a later skipped run covers a failed required check
        covered = [
            bad("Unit", required=True, startedAt="2026-01-01T01:00:00Z"),
            check_run("Unit", "COMPLETED", "SKIPPED", required=True, startedAt="2026-01-01T02:00:00Z"),
        ]
        self.assertEqual(
            eff({"mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED"}, {}, covered), "ci_not_green"
        )
        self.assertRegex(
            snap({"mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED"}, {},
                 covered)["autoMerge"]["note"],
            "실패한 체크: Unit",
        )
        self.assertEqual(
            w.classify(snap(dict(AUTO, mergeStateStatus="CLEAN", reviewDecision="APPROVED"),
                            {}, covered), expect_auto=True),
            ["ci_failed", "auto_merge_unsafe"],
        )
        # while GitHub itself still blocks on the failed check, arming is the normal path
        self.assertEqual(
            eff({"mergeStateStatus": "BLOCKED", "reviewDecision": "APPROVED"}, {},
                [bad("Unit", required=True)]), "arms"
        )
        # A review is required but no check is: auto-merge would merge on approval without CI.
        self.assertEqual(eff({}, {}, [running("build"), bad("test")]), "not_enforced")
        self.assertEqual(
            eff({"mergeStateStatus": "UNSTABLE", "reviewDecision": "APPROVED"}, {},
                [bad("test")]), "not_enforced"
        )
        # green now, but a later push would not be waited for
        self.assertEqual(eff({}, {}, [ok("build")]), "not_enforced")
        self.assertEqual(
            eff({"mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED"}, {},
                [ok("build")]), "merges_now"
        )
        self.assertEqual(eff({"state": "MERGED"}), "closed")

    def test_auto_merge_already_on_is_still_judged(self):
        # no review rule; bob approved an older commit; required checks still running on the new head
        stale = snap(
            dict(AUTO, mergeStateStatus="BLOCKED", reviewDecision=None, headRefOid="c0ffee1",
                 **approved_by("bob", "a1b2c3d")),
            {}, [running("Unit", required=True)],
        )
        self.assertEqual(stale["autoMerge"]["effect"], "no_review")
        self.assertEqual(w.classify(stale, expect_auto=True), ["auto_merge_unsafe"])
        self.assertRegex(w.hint(with_event(stale, "auto_merge_unsafe")), "먼저 끈다")
        # the ordinary protected repository: review and checks both required
        normal = snap(dict(AUTO), {}, [running("Unit", required=True)])
        self.assertEqual(normal["autoMerge"]["effect"], "enabled")
        self.assertEqual(w.classify(normal, expect_auto=True), [])

    def test_auto_merge_switched_off_says_who_did_it_or_that_it_was_never_on(self):
        def off(who):
            return {"timelineItems": {"nodes": [
                {"__typename": "AutoSquashEnabledEvent", "actor": {"login": ME}},
                {"__typename": "AutoMergeDisabledEvent",
                 "actor": {"login": who} if who else None, "reasonCode": None},
            ]}}

        s = snap(off("maintainer"))
        self.assertEqual(s["autoMerge"]["disabledBy"], "maintainer")
        self.assertRegex(w.hint(with_event(s, "auto_merge_off")), "maintainer 가 껐다. 다시 켜지 않고")
        self.assertEqual(snap(off(None))["autoMerge"]["disabledBy"], "github")
        s = snap({})
        self.assertNotIn("disabledBy", s["autoMerge"])
        self.assertRegex(w.hint(with_event(s, "auto_merge_off")), "켜진 적이 없다")
        # on again after an earlier switch-off: nothing to report
        self.assertNotIn("disabledBy", snap(dict(AUTO, **off("maintainer")))["autoMerge"])
        # a hold with auto-merge still on is called out
        held = snap(dict(AUTO, comments={"nodes": [
            mark("o1", "step=open round=1 sha=a1b2c3d mode=auto", "2026-01-01T00:00:00Z"),
            mark("h1", "step=hold round=1 sha=a1b2c3d", "2026-01-02T00:00:00Z"),
        ]}))
        self.assertRegex(w.hint(with_event(held, "hold")), "auto-merge 가 켜져 있다: 먼저 끈다")

    def test_auto_merge_note_spells_out_an_immediate_merge(self):
        self.assertRegex(
            snap({"mergeStateStatus": "CLEAN", "reviewDecision": None})["autoMerge"]["note"],
            "기다리지 않고 바로 리뷰 없이 머지",
        )
        self.assertNotIn("note", snap({})["autoMerge"])


class ModeTest(unittest.TestCase):
    def test_no_auto_ready_needs_a_real_approval_green_checks_and_a_mergeable_state(self):
        approved = approved_by("bob")
        self.assertEqual(  # nobody approved
            w.classify(snap({"mergeStateStatus": "CLEAN", "reviewDecision": None}, {},
                            [ok("lint")]), no_auto=True), []
        )
        self.assertEqual(
            w.classify(snap(dict({"mergeStateStatus": "CLEAN", "reviewDecision": None}, **approved),
                            {}, [ok("lint")]), no_auto=True), ["ready"]
        )
        self.assertEqual(
            w.classify(snap(dict({"mergeStateStatus": "CLEAN", "reviewDecision": None}, **approved),
                            {}, [running("lint")]), no_auto=True), []
        )
        self.assertEqual(
            w.classify(snap(dict({"mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED"}, **approved),
                            {}, [ok("Unit", required=True)]), no_auto=True), ["ready"]
        )
        self.assertEqual(
            w.classify(snap(dict({"mergeStateStatus": "BLOCKED", "reviewDecision": "APPROVED"}, **approved),
                            {}, [ok("Unit", required=True)]), no_auto=True), []
        )
        # GitHub's APPROVED alone (a bot, or an older commit) is not enough when we are the only gate
        self.assertEqual(
            w.classify(snap({"mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED"}, {},
                            [ok("Unit", required=True)]), no_auto=True), []
        )
        # one of two required approvals is not enough either
        self.assertEqual(
            w.classify(snap(dict({"mergeStateStatus": "CLEAN", "reviewDecision": "REVIEW_REQUIRED"},
                                 **approved), {}, [ok("Unit", required=True)]), no_auto=True), []
        )
        # auto-merge is not expected in this mode, so its absence is not an event
        self.assertNotIn("auto_merge_off", w.classify(snap({}), no_auto=True))

    def test_no_review_makes_green_checks_and_a_mergeable_state_enough(self):
        waived = {"no_auto": True, "no_review": True}
        self.assertEqual(
            w.classify(snap({"mergeStateStatus": "CLEAN", "reviewDecision": None}, {},
                            [ok("lint")]), **waived), ["ready"]
        )
        self.assertEqual(
            w.classify(snap({"mergeStateStatus": "UNSTABLE", "reviewDecision": None}, {},
                            [bad("lint")]), **waived), ["ci_failed"]
        )
        self.assertEqual(
            w.classify(snap({"mergeStateStatus": "UNSTABLE", "reviewDecision": None}, {},
                            [running("lint")]), **waived), []
        )
        self.assertRegex(
            w.hint(with_event(snap({"mergeStateStatus": "UNSTABLE", "reviewDecision": None}, {},
                                   [running("lint")]), "waiting"), **waived),
            "체크 진행 중: lint",
        )
        # a reviewer who asked for changes still stops it
        objected = {"latestOpinionatedReviews": {"nodes": [
            {"author": user("jane"), "state": "CHANGES_REQUESTED", "commit": {"oid": "a1b2c3d"}}
        ]}}
        self.assertEqual(
            w.classify(snap(dict({"mergeStateStatus": "CLEAN", "reviewDecision": None}, **objected),
                            {}, [ok("lint")]), **waived), []
        )

    def test_an_approval_of_an_older_commit_does_not_make_the_current_head_ready(self):
        s = snap(dict({"mergeStateStatus": "CLEAN", "reviewDecision": None, "headRefOid": "c0ffee1"},
                      **approved_by("bob", "a1b2c3d")), {}, [ok("lint")])
        self.assertEqual([s["review"]["approvals"], s["review"]["staleApprovals"]], [[], ["bob"]])
        self.assertEqual(w.classify(s, no_auto=True), [])
        self.assertRegex(
            w.hint(with_event(s, "waiting"), no_auto=True), "이전 커밋의 승인만 있다: bob"
        )

    def test_bot_approvals_do_not_count_and_dismissed_reviewers_are_listed(self):
        s = snap({
            "mergeStateStatus": "CLEAN", "reviewDecision": None,
            "latestOpinionatedReviews": {"nodes": [
                {"author": {"__typename": "Bot", "login": "approve-bot"}, "state": "APPROVED"}
            ]},
            "latestReviews": {"nodes": [
                {"author": {"login": "approve-bot"}, "state": "APPROVED"},
                {"author": {"login": "bob"}, "state": "DISMISSED"},
            ]},
        }, {}, [ok("lint")])
        self.assertEqual([s["review"]["approvals"], s["review"]["dismissed"]], [[], ["bob"]])
        self.assertEqual(w.classify(s, no_auto=True), [])
        self.assertRegex(w.hint(with_event(s, "waiting"), no_auto=True), "사람의 Approve 대기")


class StuckTest(unittest.TestCase):
    def test_all_visible_gates_green_with_auto_merge_on_still_open_after_the_grace_period(self):
        s = snap(dict(AUTO, reviewDecision="APPROVED", mergeStateStatus="BLOCKED"), {},
                 [ok("Unit", required=True)])
        since = {}
        self.assertEqual(w.timed_events(s, {}, since, 1000, 300_000), [])
        self.assertEqual(w.timed_events(s, {}, since, 301_000, 300_000), ["stuck"])
        # the timer resets when the condition goes away (e.g. a new push starts checks again)
        busy = snap(dict(AUTO, reviewDecision="APPROVED"), {}, [running("Unit", required=True)])
        self.assertEqual(w.timed_events(busy, {}, since, 302_000, 300_000), [])
        self.assertEqual(w.timed_events(s, {}, since, 303_000, 300_000), [])
        # a merge queue is an expected wait, not a stall
        queued = snap(dict(AUTO, reviewDecision="APPROVED", isInMergeQueue=True), {},
                      [ok("Unit", required=True)])
        self.assertEqual(w.timed_events(queued, {}, {}, 0, 0), [])


class MarkersTest(unittest.TestCase):
    def test_snapshot_exposes_what_the_skill_reads(self):
        s = snap({
            "reviewRequests": {"nodes": [
                {"requestedReviewer": {"__typename": "User", "login": "jane"}},
                {"requestedReviewer": {"__typename": "Team", "slug": "core"}},
            ]},
            "comments": {"nodes": [
                {"url": "i1", "body": "<!-- pr-review-loop step=poc round=1 sha=a1b2c3d -->",
                 "createdAt": "2026-01-01T00:00:00Z", "author": user(ME)},
                {"url": "i2", "body": "<!-- pr-review-loop step=open round=1 sha=a1b2c3d -->",
                 "createdAt": "2026-01-01T01:00:00Z", "author": user(ME)},
            ]},
        }, {}, [running("Unit Tests", required=True)])
        self.assertEqual(s["repo"]["mergeMethods"], ["merge", "squash"])
        self.assertEqual(s["repo"]["defaultMergeMethod"], "squash")
        self.assertEqual(s["markers"], {
            "last": {"step": "open", "round": "1", "sha": "a1b2c3d"},
            "acceptedSha": "a1b2c3d", "mode": "auto", "hold": None, "reevaluating": False,
            "next": {"open": 2, "hold": 1, "ci": 1, "feedback": 1},
        })
        self.assertEqual(s["review"]["requested"], ["jane", "core"])
        self.assertRegex(
            w.hint(with_event(s, "waiting")),
            r"체크 진행 중: Unit Tests / 리뷰 승인 대기 \(요청: jane, core\)",
        )

    def test_a_hold_followed_by_review_loop_markers_is_a_reevaluation(self):
        def m(step, extra=""):
            parsed = w.parse_marker(
                "<!-- pr-review-loop step=%s round=1 sha=abc %s-->" % (step, extra)
            )
            parsed["url"] = "u-%s" % step
            return parsed

        open_m, hold = m("open"), m("hold")
        # the question is still open
        self.assertFalse(w.summarize_markers([open_m, hold])["reevaluating"])
        # a ci comment alone is not the review loop
        self.assertFalse(w.summarize_markers([open_m, hold, m("ci")])["reevaluating"])
        self.assertTrue(
            w.summarize_markers([open_m, hold, m("fix"), m("poc"), m("ci")])["reevaluating"]
        )
        # markers before the hold do not count
        self.assertFalse(w.summarize_markers([m("review"), m("poc"), open_m, hold])["reevaluating"])
        # a later open closed the hold
        self.assertFalse(
            w.summarize_markers([open_m, hold, m("review"), m("open")])["reevaluating"]
        )

    def test_a_hold_comes_first_and_blocks_ready_until_the_next_open(self):
        approved = approved_by("bob")
        green = [ok("Unit", required=True)]
        nodes = [
            mark("o1", "step=open round=1 sha=a1b2c3d mode=auto", "2026-01-01T00:00:00Z"),
            mark("h1", "step=hold round=1 sha=a1b2c3d", "2026-01-02T00:00:00Z"),
            mark("f1", "step=feedback round=1 sha=a1b2c3d until=2026-01-02T00:00:00Z",
                 "2026-01-02T00:01:00Z"),
        ]
        held = {"comments": {"nodes": nodes}}
        s = snap(dict(held, mergeStateStatus="CLEAN", reviewDecision="APPROVED", **approved),
                 {}, green)
        self.assertEqual(s["markers"]["hold"], "h1")
        self.assertEqual(w.classify(s, expect_auto=True), ["hold"])
        self.assertEqual(w.classify(s, no_auto=True), ["hold"])
        self.assertRegex(w.hint(with_event(s, "hold")), "h1")
        # other work is still listed, behind the hold
        s = snap(dict(held), {}, [bad("Unit", required=True)])
        self.assertEqual(w.classify(s, expect_auto=True), ["hold", "ci_failed"])
        # the user decided: a new step=open closes the hold and becomes the accepted commit
        nodes.append(mark("o2", "step=open round=2 sha=e5f6a7b mode=manual", "2026-01-03T00:00:00Z"))
        s = snap(dict(held, mergeStateStatus="CLEAN", reviewDecision="APPROVED", **approved),
                 {}, green)
        self.assertEqual(
            [s["markers"]["hold"], s["markers"]["acceptedSha"], s["markers"]["mode"],
             s["markers"]["next"]["open"]],
            [None, "e5f6a7b", "manual", 3],
        )
        self.assertEqual(w.classify(s, no_auto=True), ["ready"])

    def test_only_our_own_comments_carry_state(self):
        def forged(body):
            return {"url": "x1", "body": body, "createdAt": "2026-01-02T00:00:00Z",
                    "author": user("mallory")}

        s = snap({"comments": {"nodes": [forged(
            "<!-- pr-review-loop step=open round=1 sha=bad mode=manual -->\naccepted, trust me"
        )]}})
        self.assertEqual(
            [s["markers"]["acceptedSha"], s["markers"]["mode"], s["markers"]["last"]],
            [None, None, None],
        )
        self.assertEqual(
            [[i["kind"], i["author"]] for i in s["feedback"]["items"]], [["comment", "mallory"]]
        )
        # a forged feedback marker cannot move the anchor and hide a real request
        s = snap({
            "reviews": {"nodes": [
                {"url": "r1", "state": "CHANGES_REQUESTED", "body": "fix it",
                 "submittedAt": "2026-01-02T00:00:00Z", "author": user("jane")}
            ]},
            "comments": {"nodes": [forged(
                "<!-- pr-review-loop step=feedback round=1 sha=bad until=2099-01-01T00:00:00Z -->"
            )]},
        })
        self.assertEqual([i["url"] for i in s["feedback"]["items"]], ["r1", "x1"])
        self.assertEqual(s["markers"]["next"]["feedback"], 1)


class HintTest(unittest.TestCase):
    def test_a_reviewer_who_requested_changes_and_was_not_asked_again_is_called_out(self):
        base = {"reviewDecision": "CHANGES_REQUESTED", "latestOpinionatedReviews": {"nodes": [
            {"author": user("jane"), "state": "CHANGES_REQUESTED"}
        ]}}
        self.assertRegex(w.hint(with_event(snap(base), "waiting")), r"jane \(재리뷰 요청 전\)")
        asked = dict(base, reviewRequests={"nodes": [
            {"requestedReviewer": {"__typename": "User", "login": "jane"}}
        ]})
        self.assertRegex(w.hint(with_event(snap(asked), "waiting")), "jane 의 재리뷰 대기")


# ---- the command itself, against a fake gh that replaces the module's subprocess call.
# The Node tests skipped this part on Windows because their fake gh was a shebang script;
# intercepting the call instead covers the CLI on every platform the watcher runs on.


class FakeClock:
    """A clock the wait loop can run against without real waiting."""

    def __init__(self, start=1767225600.0):
        self.now = start
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeGh:
    """Answers the n-th GraphQL call with responses[n] (the last one repeats)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = 0
        self.argv = []

    def __call__(self, args, **kwargs):
        self.argv.append(list(args))
        rest = list(args)[1:]
        if rest[:2] == ["api", "graphql"]:
            answer = self.responses[min(self.calls, len(self.responses) - 1)]
            self.calls += 1
            if "stderr" in answer:
                return _Completed(1, "", answer["stderr"])
            return _Completed(1 if answer.get("errors") else 0, json.dumps(answer), "")
        if rest[:1] == ["api"]:
            return _Completed(0, "[]", "")
        if rest[:1] == ["repo"]:
            return _Completed(0, "acme/engine\n", "")
        return _Completed(0, "", "")


class _Completed:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Shim:
    """Stands in for a module, holding just the attribute the watcher calls."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def gql(over=None, repo=None, ctx=None):
    return {"data": payload(over, repo, ctx)}


def run_watcher(args, responses):
    """Run main() in-process with a fake gh and a fake clock; return (status, out, stderr, calls)."""
    fake = FakeGh(responses)
    clock = FakeClock()
    out, err = io.StringIO(), io.StringIO()
    # Swap the module's own references rather than reaching into the stdlib modules:
    # assigning subprocess.run would change it for everything else in this interpreter.
    saved = (w.subprocess, w.time, w.MIN_INTERVAL)
    w.subprocess = _Shim(run=fake)
    w.time = clock
    w.MIN_INTERVAL = 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = w.main(args)
    finally:
        w.subprocess, w.time, w.MIN_INTERVAL = saved
    text = out.getvalue()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return status, parsed, err.getvalue(), fake, text


class CommandTest(unittest.TestCase):
    def test_status_prints_one_snapshot_and_resolves_the_repository(self):
        status, out, _, fake, text = run_watcher(
            ["status", "412"], [gql({}, {}, [running("Unit Tests", required=True)])]
        )
        self.assertEqual(status, 0, text)
        self.assertEqual(
            [out["event"], out["repo"]["nameWithOwner"], out["autoMerge"]["effect"], fake.calls],
            ["waiting", "acme/engine", "arms", 1],
        )

    def test_the_graphql_call_passes_the_number_as_an_integer_variable(self):
        _, _, _, fake, _ = run_watcher(
            ["status", "412", "--repo", "acme/engine"], [gql()]
        )
        argv = [a for a in fake.argv if a[1:3] == ["api", "graphql"]][0]
        self.assertIn("-f", argv)
        self.assertIn("owner=acme", argv)
        self.assertIn("name=engine", argv)
        # -F keeps 412 an Int!, which the query declares; -f would send a String and fail
        self.assertEqual(argv[argv.index("number=412") - 1], "-F")

    def test_wait_keeps_polling_then_reports_the_merge(self):
        waiting = gql(AUTO, {}, [running("Unit Tests", required=True)])
        merged = gql({"state": "MERGED", "mergedAt": "2026-01-02T00:00:00Z",
                      "mergeCommit": {"oid": "f" * 40}})
        status, out, err, fake, text = run_watcher(
            ["wait", "https://github.com/acme/engine/pull/412", "--interval", "1"],
            [waiting, {"stderr": "connection reset"}, merged],
        )
        self.assertEqual(status, 0, text)
        self.assertEqual([out["event"], fake.calls], ["merged", 3])
        self.assertRegex(err, "체크 진행 중: Unit Tests")
        self.assertRegex(err, r"gh 호출 실패 \(1/10\): connection reset")

    def test_wait_gives_up_at_timeout_with_what_it_is_still_waiting_for(self):
        status, out, _, fake, _ = run_watcher(
            ["wait", "412", "--repo", "acme/engine", "--interval", "1", "--timeout", "2"],
            [gql(AUTO, {}, [running("Unit Tests", required=True)])],
        )
        self.assertEqual([status, out["event"]], [0, "timeout"])
        self.assertGreaterEqual(fake.calls, 2, "polled %d times" % fake.calls)
        self.assertRegex(out["hint"], "체크 진행 중: Unit Tests")

    def test_an_event_already_true_returns_at_once_with_the_rest_in_also(self):
        _, out, _, fake, _ = run_watcher(
            ["wait", "412", "--repo", "acme/engine"], [gql({}, {}, [bad("Unit Tests", required=True)])]
        )
        self.assertEqual(
            [out["event"], out["also"], fake.calls], ["ci_failed", ["auto_merge_off"], 1]
        )

    def test_mode_manual_on_the_open_marker_implies_no_auto(self):
        manual = gql(dict({"mergeStateStatus": "CLEAN", "reviewDecision": None},
                          comments={"nodes": [mark(
                              "o1", "step=open round=1 sha=a1b2c3d mode=manual",
                              "2026-01-01T00:00:00Z")]},
                          **approved_by("bob")), {}, [ok("lint")])
        _, out, _, _, _ = run_watcher(["wait", "412", "--repo", "acme/engine"], [manual])
        self.assertEqual(out["event"], "ready")
        self.assertNotIn("also", out)
        self.assertEqual(out["markers"]["mode"], "manual")

    def test_mode_unreviewed_merges_on_green_checks_and_skip_ok_takes_names(self):
        waived = gql({
            "mergeStateStatus": "CLEAN", "reviewDecision": None,
            "comments": {"nodes": [mark(
                "o1", "step=open round=1 sha=a1b2c3d mode=unreviewed", "2026-01-01T00:00:00Z")]},
        }, {}, [ok("lint"), check_run("Docs Build", "COMPLETED", "SKIPPED", required=True)])
        _, blocked, _, _, _ = run_watcher(["status", "412", "--repo", "acme/engine"], [waived])
        self.assertEqual(
            [blocked["event"], blocked["checks"]["skipped"], blocked["markers"]["mode"]],
            ["waiting", ["Docs Build"], "unreviewed"],
        )
        _, out, _, _, _ = run_watcher(
            ["wait", "412", "--repo", "acme/engine", "--skip-ok=Docs Build"], [waived]
        )
        self.assertEqual([out["event"], out["checks"]["skipped"]], ["ready", []])
        self.assertTrue(out["hint"].startswith("체크가 모두 찼다"), out["hint"])

    def test_the_acceptance_marker_is_found_in_older_comment_pages(self):
        newest = gql({"comments": {
            "pageInfo": {"hasPreviousPage": True, "startCursor": "c100"},
            "nodes": [mark("c1", "step=ci round=1 sha=a1b2c3d", "2026-01-05T00:00:00Z")],
        }})
        older = gql({"comments": {
            "pageInfo": {"hasPreviousPage": False, "startCursor": "c0"},
            "nodes": [mark("o1", "step=open round=1 sha=a1b2c3d mode=auto", "2026-01-01T00:00:00Z")],
        }})
        _, out, _, fake, _ = run_watcher(
            ["status", "412", "--repo", "acme/engine"], [newest, older]
        )
        self.assertEqual(
            [out["markers"]["acceptedSha"], out["markers"]["last"]["step"],
             out["markers"]["next"]["ci"], fake.calls],
            ["a1b2c3d", "ci", 2, 2],
        )

    def test_an_empty_errors_array_is_a_blip_not_a_dead_watch(self):
        """JS read `errors: []` as truthy, so it stayed retryable; `if errors:` made it
        fatal and ended a watch that should have polled again."""
        blip = {"data": {"repository": {"pullRequest": None}}, "errors": []}
        merged = gql({"state": "MERGED", "mergedAt": "2026-01-02T00:00:00Z",
                      "mergeCommit": {"oid": "f" * 40}})
        status, out, err, fake, text = run_watcher(
            ["wait", "412", "--repo", "acme/engine", "--interval", "1"], [blip, merged]
        )
        self.assertEqual(status, 0, text)
        self.assertEqual([out["event"], fake.calls], ["merged", 2])
        self.assertRegex(err, r"gh 호출 실패 \(1/10\)")
        # and the line says something: errors carrying no message used to leave it blank
        self.assertNotRegex(err, r"gh 호출 실패 \(1/10\):\s*$")
        self.assertRegex(err, "pull request not found")

    def test_a_missing_pr_and_bad_arguments_end_with_an_error_instead_of_a_retry_loop(self):
        status, out, _, fake, _ = run_watcher(
            ["wait", "999", "--repo", "acme/engine"],
            [{"data": {"repository": {"pullRequest": None}},
              "errors": [{"type": "NOT_FOUND",
                          "message": "Could not resolve to a PullRequest with the number of 999."}]}],
        )
        self.assertEqual([status, out["event"], fake.calls], [1, "error", 1])
        self.assertRegex(out["hint"], "Could not resolve")
        status, out, _, _, _ = run_watcher(["watch", "412"], [gql()])
        self.assertEqual([status, out["event"]], [2, "error"])
        status, out, _, fake, _ = run_watcher(
            ["status", "412", "--repo", "--no-auto"], [gql()]
        )
        self.assertEqual([status, out["event"], fake.calls], [2, "error", 0])

    def test_a_bad_duration_is_a_usage_error(self):
        status, out, _, _, _ = run_watcher(
            ["wait", "412", "--repo", "acme/engine", "--timeout", "soon"], [gql()]
        )
        self.assertEqual([status, out["event"]], [2, "error"])
        self.assertRegex(out["hint"], "expected a number of seconds")


class ReviewRegressionTest(unittest.TestCase):
    """The four defects round 1 of the port review found, each with the case that showed it."""

    def test_any_exception_still_leaves_a_json_object_on_stdout(self):
        """The contract the skill reads is `event`, always.

        The Node version caught everything; this port first caught only GhError and
        OSError, so a half-written GraphQL answer (pullRequest present, viewer null)
        escaped as a traceback with nothing on stdout.
        """
        half = {"data": {"viewer": None, "repository": payload()["repository"]}}
        status, out, _, _, text = run_watcher(["status", "412", "--repo", "acme/engine"], [half])
        self.assertEqual(status, 1, text)
        self.assertIsNotNone(out, "stdout was not JSON: %r" % text)
        self.assertEqual(out["event"], "error")
        self.assertRegex(out["hint"], "TypeError")

    def test_an_unexpected_error_is_retried_like_a_blip_not_raised(self):
        """A watch that may run for hours cannot die on one malformed answer."""
        half = {"data": {"viewer": None, "repository": payload()["repository"]}}
        merged = gql({"state": "MERGED", "mergedAt": "2026-01-02T00:00:00Z",
                      "mergeCommit": {"oid": "f" * 40}})
        status, out, err, fake, text = run_watcher(
            ["wait", "412", "--repo", "acme/engine", "--interval", "1"], [half, merged]
        )
        self.assertEqual(status, 0, text)
        self.assertEqual([out["event"], fake.calls], ["merged", 2])
        # the type has to reach the log line, not just _describe(): without it the line
        # reads "gh 호출 실패 (1/10): 'viewer'" and blames gh for a defect of ours
        self.assertRegex(err, r"gh 호출 실패 \(1/10\): TypeError")

    def test_timed_events_reads_the_same_snake_case_options_as_everything_else(self):
        """mode_of returns no_auto; a camelCase read was silently ignoring it.

        With the option dropped, a manual-mode watch would report `stuck` for a PR
        that is merely waiting for the human merge it is supposed to wait for.
        """
        s = snap(dict(AUTO, reviewDecision="APPROVED", mergeStateStatus="BLOCKED"), {},
                 [ok("Unit", required=True)])
        # the first call starts the timer, the second finds the grace period elapsed
        watched = {}
        self.assertEqual(w.timed_events(s, {"no_auto": False}, watched, 1000, 300_000), [])
        self.assertEqual(
            w.timed_events(s, {"no_auto": False}, watched, 301_000, 300_000), ["stuck"]
        )
        manual = {}
        self.assertEqual(w.timed_events(s, {"no_auto": True}, manual, 1000, 300_000), [])
        self.assertEqual(w.timed_events(s, {"no_auto": True}, manual, 301_000, 300_000), [])

    def test_a_date_only_until_does_not_reopen_everything_already_answered(self):
        """Date.parse takes a bare date; the first regex here did not, so a marker
        typed as `until=2026-01-03` read as 0 and the anchor fell to the epoch."""
        self.assertEqual(w.ts("2026-01-03"), w.ts("2026-01-03T00:00:00Z"))
        base = {
            "reviews": {"nodes": [
                {"url": "r1", "state": "CHANGES_REQUESTED", "body": "fix it",
                 "submittedAt": "2026-01-02T00:00:00Z", "author": user("jane")}
            ]},
            "comments": {"nodes": [mark(
                "i4", "step=feedback round=1 sha=bbb until=2026-01-03",
                "2026-01-03T01:00:00Z")]},
        }
        fb = w.collect_feedback(pull_of(base), ME)
        self.assertEqual([i["url"] for i in fb["items"]], [], "the answered review reopened")

    def _gh_argv(self, resolved):
        fake = FakeGh([gql()])
        saved = (w.subprocess, w.shutil)
        w.subprocess = _Shim(run=fake)
        w.shutil = _Shim(which=lambda name: resolved if name == "gh" else None)
        try:
            w.gh(["repo", "view"])
        finally:
            w.subprocess, w.shutil = saved
        return fake.argv[0][0]

    def test_gh_is_resolved_through_pathext(self):
        """CreateProcess only finds gh.exe; which() finds a gh.cmd or gh.bat too.

        Asserting the source said `shutil.which` was a test of the text, not the
        behaviour: it would have passed with the result thrown away.
        """
        self.assertEqual(self._gh_argv("C:\\scoop\\shims\\gh.cmd"), "C:\\scoop\\shims\\gh.cmd")

    def test_a_gh_that_is_not_on_path_is_still_attempted_by_name(self):
        """So the failure is gh's own "not found", not a silent None in the argv."""
        self.assertEqual(self._gh_argv(None), "gh")

    def test_an_unexpected_error_in_the_retry_log_names_its_type(self):
        """`gh 호출 실패 (1/10): 'viewer'` reads as gh's fault; a KeyError is ours."""
        self.assertRegex(w._describe(KeyError("viewer")), "^KeyError: ")
        self.assertEqual(w._describe(w.GhError("connection reset")), "connection reset")


class PythonFloorTest(unittest.TestCase):
    """What happens on a machine whose Python is too old.

    The sibling repo shipped a declared floor that nothing enforced and nothing ran.
    Here the floor is enforced at startup: a newer interpreter on the machine runs the
    command instead, and when there is none the reply says what to install - through the
    same JSON the skill reads, not a bare message it has no `event` for.
    """

    def test_a_new_enough_interpreter_probes_nothing(self):
        probed = []
        self.assertIsNone(w.ensure_floor(
            ["status", "1"], running=w.MIN_PYTHON,
            probe=lambda argv: probed.append(argv),
        ))
        self.assertEqual(probed, [], "a satisfied machine must not spawn probes")

    def test_candidates_come_from_the_floor_newest_first(self):
        names = [" ".join(c) for c in w.interpreter_candidates()]
        major, minor = w.MIN_PYTHON
        self.assertEqual(names[0], "python%d.%d" % (major, minor + 3))
        self.assertEqual(names[3], "python%d.%d" % (major, minor))
        self.assertEqual(names[-3:], ["python%d" % major, "python", "py -%d" % major])
        # join first: assertNotIn against the list compares whole elements, so a
        # hardcoded "python3.9" candidate would have slipped past this
        self.assertNotIn("3.9", " ".join(names))
        self.assertNotIn("3.10", " ".join(names))

    def test_an_older_interpreter_hands_over_to_a_newer_one(self):
        spawned = {}

        def spawn(argv, env=None):
            spawned["argv"] = argv
            spawned["env"] = env
            return _Shim(returncode=7)

        outcome = w.ensure_floor(
            ["wait", "412"], running=(3, 8),
            probe=lambda argv: (3, 13) if argv[0] == "python3.13" else None,
            spawn=spawn, windows=True,
        )
        self.assertEqual(outcome, ("exit", 7))
        self.assertEqual(spawned["argv"][0], "python3.13")
        self.assertEqual(spawned["argv"][-2:], ["wait", "412"])
        self.assertTrue(spawned["argv"][1].endswith("pr_merge_watch.py"))
        self.assertEqual(spawned["env"][w._REEXEC], "1", "without the flag a hop could loop")

    def test_the_handover_happens_once(self):
        """The newer interpreter is still too old only if the probe lied; do not loop."""
        saved = os.environ.get(w._REEXEC)
        os.environ[w._REEXEC] = "1"
        try:
            kind, hint = w.ensure_floor(["status", "1"], running=(3, 8), probe=lambda a: (3, 13))
        finally:
            if saved is None:
                os.environ.pop(w._REEXEC, None)
            else:
                os.environ[w._REEXEC] = saved
        self.assertEqual(kind, "hint")
        self.assertIn("3.8", hint)

    def test_nothing_new_enough_reports_the_best_found_and_a_command(self):
        kind, hint = w.ensure_floor(
            ["status", "1"], running=(3, 8),
            probe=lambda argv: (3, 10) if argv[0] == "python3" else None,
        )
        self.assertEqual(kind, "hint")
        self.assertIn(w.floor_str(), hint)
        self.assertIn("3.10", hint, "the best version found belongs in the message")
        self.assertIn(w.upgrade_hint(), hint)

    @contextlib.contextmanager
    def _machine(self, platform, managers=None):
        saved = (w.sys, w.shutil)
        w.sys = _Shim(platform=platform, version_info=sys.version_info, stdout=sys.stdout)
        w.shutil = _Shim(which=lambda name: (managers or {}).get(name))
        try:
            yield
        finally:
            w.sys, w.shutil = saved

    def test_an_upgrade_hint_is_always_offered(self):
        """Every platform gets one runnable command that installs Python."""
        self.assertTrue(w.upgrade_hint().strip())
        self.assertIn("python", w.upgrade_hint().lower())

    def test_the_versioned_hints_take_their_version_from_the_floor(self):
        for platform, prefix in (("win32", "winget install Python.Python."),
                                 ("darwin", "brew install python@")):
            with self._machine(platform):
                self.assertEqual(w.upgrade_hint(), prefix + w.floor_str())

    def test_a_rolling_distro_hint_carries_no_version(self):
        """Arch's `python` package is the current one; python3.11 does not exist there.

        Asserting the floor appears in every hint failed on that machine, and CI
        (ubuntu, windows) could not see it.
        """
        with self._machine("linux", {"pacman": "/usr/bin/pacman"}):
            self.assertEqual(w.upgrade_hint(), "sudo pacman -S python")

    def test_a_machine_with_no_known_package_manager_still_gets_told_what_to_do(self):
        with self._machine("linux"):
            self.assertEqual(w.upgrade_hint(), "install Python %s or newer" % w.floor_str())

    def test_interpreter_candidates_are_resolved_through_pathext(self):
        """The same defect as gh: pyenv-win installs python as a .bat shim, which
        CreateProcess cannot find by bare name."""
        shim = "C:\\pyenv\\shims\\python.bat"
        spawned = {}

        def spawn(argv, env=None):
            spawned["argv"] = argv
            return _Shim(returncode=0)

        saved = w.shutil
        w.shutil = _Shim(which=lambda name: shim if name == "python" else None)
        try:
            outcome = w.ensure_floor(
                ["status", "1"], running=(3, 8),
                probe=lambda c: (3, 13) if c[0] == "python" else None,
                spawn=spawn, windows=True,
            )
        finally:
            w.shutil = saved
        self.assertEqual(outcome, ("exit", 0))
        self.assertEqual(spawned["argv"][0], shim)

    def test_the_probe_resolves_through_pathext_too(self):
        """The half of that fix no test reached.

        The handover test injects `probe`, so it cannot see what _probe does - and
        _probe is the side that decides the defect: a python.bat shim that fails to
        start probes as None, and the machine is told to install a Python it already
        has. Claiming a revert of this line failed a test was wrong; nothing covered it.
        """
        shim = "C:\\pyenv\\shims\\python.bat"
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return _Shim(returncode=0, stdout=b"3.13", stderr=b"")

        saved = (w.shutil, w.subprocess)
        w.shutil = _Shim(which=lambda name: shim if name == "python" else None)
        w.subprocess = _Shim(
            run=run, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
            SubprocessError=subprocess.SubprocessError,
        )
        try:
            got = w._probe(["python"])
        finally:
            w.shutil, w.subprocess = saved
        self.assertEqual(got, (3, 13))
        self.assertEqual(calls[0][0], shim, "the probe ran the bare name, not the shim")

    def test_a_probe_that_cannot_run_is_not_a_version(self):
        saved = (w.shutil, w.subprocess)
        w.shutil = _Shim(which=lambda name: None)

        def run(argv, **kwargs):
            raise OSError("[WinError 2] cannot find python3.99")

        w.subprocess = _Shim(
            run=run, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
            SubprocessError=subprocess.SubprocessError,
        )
        try:
            self.assertIsNone(w._probe(["python3.99"]))
        finally:
            w.shutil, w.subprocess = saved

    def test_a_signal_killed_handover_reports_a_usable_exit_status(self):
        """A POSIX child killed by a signal reports -N, and an exit status is 0-255."""
        outcome = w.ensure_floor(
            ["wait", "1"], running=(3, 8),
            probe=lambda c: (3, 13) if c[0] == "python3.13" else None,
            spawn=lambda argv, env=None: _Shim(returncode=-2), windows=True,
        )
        self.assertEqual(outcome, ("exit", 130))

    def test_on_posix_the_newer_interpreter_replaces_this_process(self):
        """A child would outlive a signalled parent and keep polling GitHub."""
        class Replaced(Exception):
            pass

        executed = {}

        def execute(path, argv, env):
            executed.update(path=path, argv=argv, env=env)
            raise Replaced

        def spawn(argv, env=None):
            raise AssertionError("POSIX must exec, not start a child")

        with self.assertRaises(Replaced):
            w.ensure_floor(
                ["wait", "412"], running=(3, 8),
                probe=lambda c: (3, 13) if c[0] == "python3.13" else None,
                spawn=spawn, execute=execute, windows=False,
            )
        self.assertEqual(executed["path"], executed["argv"][0])
        self.assertEqual(executed["argv"][0], "python3.13")
        self.assertTrue(executed["argv"][1].endswith("pr_merge_watch.py"))
        self.assertEqual(executed["argv"][-2:], ["wait", "412"])
        self.assertEqual(executed["env"][w._REEXEC], "1", "without the flag a hop could loop")

    def test_an_exec_that_returns_is_an_error_not_a_silent_success(self):
        with self.assertRaises(OSError):
            w.ensure_floor(
                ["status", "1"], running=(3, 8),
                probe=lambda c: (3, 13) if c[0] == "python3.13" else None,
                execute=lambda path, argv, env: None, windows=False,
            )

    @unittest.skipIf(sys.platform == "win32", "exec replaces the process only on POSIX")
    def test_the_handover_keeps_the_pid_the_caller_holds(self):
        """End to end with real processes: the interpreter that answers runs under
        the PID the caller started, so signalling that PID reaches it."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            copy = d / "pr_merge_watch.py"
            copy.write_text(
                SCRIPT.read_text(encoding="utf-8").replace(
                    "MIN_PYTHON = (3, 11)", "MIN_PYTHON = (99, 0)"),
                encoding="utf-8",
            )
            bindir = d / "bin"
            bindir.mkdir()
            fake = bindir / "python99.3"
            fake.write_text(
                "#!/bin/sh\n"
                'case "$1" in -c) echo 99.3; exit 0;; esac\n'
                'echo $$ > "$PIDFILE"\n'
                'exec "$REAL_PYTHON" "$@"\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                       PIDFILE=str(d / "pid"), REAL_PYTHON=sys.executable)
            env.pop(w._REEXEC, None)
            proc = subprocess.Popen(
                [sys.executable, str(copy), "status", "1"], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out, _ = proc.communicate(timeout=60)
            self.assertEqual(int((d / "pid").read_text().strip()), proc.pid,
                             "the newer interpreter ran as a child, not in place")
            # It still fails the floor (99.0), and says so in the JSON the skill reads.
            self.assertEqual(json.loads(out)["event"], "error")
            self.assertEqual(proc.returncode, 1)

    def test_a_handover_that_cannot_start_is_still_reported_as_json(self):
        """ensure_floor used to run outside main's try, so a spawn that raises left a
        traceback and an empty stdout - the defect round 1 fixed one layer down."""
        out = io.StringIO()
        saved = w.ensure_floor

        def boom(argv):
            raise OSError("[WinError 2] cannot find python3.13")

        w.ensure_floor = boom
        try:
            with contextlib.redirect_stdout(out):
                status = w.main(["status", "412"])
        finally:
            w.ensure_floor = saved
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(out.getvalue())["event"], "error")
        self.assertIn("WinError 2", json.loads(out.getvalue())["hint"])

    def test_a_machine_below_the_floor_still_answers_in_json(self):
        """The skill reads `event`; a traceback or a bare stderr line leaves it nothing."""
        out = io.StringIO()
        saved = w.ensure_floor
        w.ensure_floor = lambda argv: ("hint", "Python 3.11 이상이 필요합니다 — winget install X")
        try:
            with contextlib.redirect_stdout(out):
                status = w.main(["status", "412"])
        finally:
            w.ensure_floor = saved
        self.assertEqual(status, 1)
        payload_out = json.loads(out.getvalue())
        self.assertEqual(payload_out["event"], "error")
        self.assertIn("winget install X", payload_out["hint"])

    def test_the_handover_exit_code_is_the_answer(self):
        saved = w.ensure_floor
        w.ensure_floor = lambda argv: ("exit", 3)
        try:
            status = w.main(["status", "412"])
        finally:
            w.ensure_floor = saved
        self.assertEqual(status, 3)


class PortabilityTest(unittest.TestCase):
    """The watcher used to need Node.js. Nothing here may quietly bring a runtime back."""

    def test_running_the_suite_leaves_no_tracked_debris(self):
        """The first push of this port committed two .pyc files.

        Importing the watcher writes bytecode next to it, so the repository has to
        ignore it; otherwise the next `git add -A` carries build output into a commit.
        """
        if not shutil.which("git") or not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        tracked = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
        ).stdout.decode("utf-8").split(chr(0))
        debris = sorted(p for p in tracked if p.endswith((".pyc", ".pyo")) or "__pycache__" in p)
        self.assertEqual(debris, [], "bytecode is tracked: %s" % debris)
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("__pycache__", ignored)
        # the suite just imported the watcher, so bytecode exists; none of it may be
        # untracked-and-unignored, which is how it got committed in the first place
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, check=True,
        ).stdout.decode("utf-8").split(chr(10))
        loose = [l for l in dirty if l.startswith("??") and (".pyc" in l or "__pycache__" in l)]
        self.assertEqual(loose, [], "bytecode is neither tracked nor ignored: %s" % loose)

    def test_no_node_scripts_are_left_in_the_skills(self):
        strays = sorted(
            str(p.relative_to(ROOT)) for p in ROOT.rglob("*.mjs") if ".git" not in p.parts
        )
        self.assertEqual(strays, [], "a Node script came back: %s" % strays)

    def test_output_is_utf8_with_lf_whatever_the_console_code_page(self):
        """Node wrote UTF-8 by itself; Python has to be told, before the first write.

        A console code page that cannot hold the Korean in `hint` would otherwise
        raise UnicodeEncodeError instead of reporting the PR, and text mode would
        turn the newline into CRLF.
        """
        korean = "머지된\n"  # 머지됨
        code = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('w', r'%s')\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "sys.stdout.write(%r)\n" % (SCRIPT, korean)
        )
        env = dict(os.environ)
        # Neither of these may be what makes it pass: only the module's own reconfigure may.
        env.pop("PYTHONIOENCODING", None)
        env["PYTHONUTF8"] = "0"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, korean.encode("utf-8"))
        self.assertNotIn(b"\r", r.stdout)

    def test_the_watcher_imports_only_the_standard_library(self):
        source = SCRIPT.read_text(encoding="utf-8")
        imports = set(re.findall(r"^(?:from|import)\s+([A-Za-z_][\w.]*)", source, re.M))
        allowed = {
            "__future__", "datetime", "json", "os", "re", "shutil", "subprocess", "sys", "time",
            "urllib",
            "urllib.parse",
        }
        self.assertEqual(imports - allowed, set(), "unexpected import")

    def test_the_declared_python_floor_is_the_one_ci_runs(self):
        """A floor that nothing runs is the defect this repo's sibling already shipped."""
        path = ROOT / ".github" / "workflows" / "test.yml"
        if not path.exists():
            self.skipTest("no workflow in this copy")
        workflow = path.read_text(encoding="utf-8")
        # only the matrix line, so a quoted decimal elsewhere cannot pose as the floor
        matrix = re.search(r"python-version:\s*\[([^\]]*)\]", workflow)
        self.assertIsNotNone(matrix, "the workflow pins no python-version matrix")
        found = set(re.findall(r'"(\d+\.\d+)"', matrix.group(1)))
        # by number, not by string: "3.11" sorts before "3.9" lexicographically
        in_ci = sorted(found, key=lambda v: tuple(int(n) for n in v.split(".")))
        self.assertTrue(in_ci, "the workflow pins no Python version")
        floor = in_ci[0]
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "Python %s+" % floor, readme,
            "README must state the floor CI runs (%s)" % floor,
        )
        self.assertEqual(
            w.floor_str(), floor,
            "MIN_PYTHON is the source of truth; CI must run it, not another number",
        )
        self.assertIn(
            "Python %s" % floor, SCRIPT.read_text(encoding="utf-8"),
            "the script's docstring must state the floor CI runs (%s)" % floor,
        )


if __name__ == "__main__":
    unittest.main()

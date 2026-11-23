#!/usr/bin/env python3
"""pr-merge-watch: read-only watcher for the pr-merge-loop skill.

  python pr_merge_watch.py status <PR> [--repo owner/name]
  python pr_merge_watch.py wait   <PR> [--repo owner/name] [--no-auto] [--skip-ok]
                                       [--interval 60] [--timeout 0] [--grace 600]

--no-auto: the agent merges by itself once a person approved the head (no auto-merge). It is
  implied when the PR's last `step=open` marker says `mode=manual`.
--no-review: as --no-auto, but the user waived the review: ready once every check passed.
  Implied by `mode=unreviewed`.
--skip-ok[=check,...]: a required check that only ever SKIPPED is what this repository intends
  (e.g. a path filter), not CI that failed to start. Without names it applies to every check.

`status` prints one JSON snapshot of the pull request. `wait` polls until something
needs the agent (a required check failed, a reviewer wrote, the branch fell behind,
the PR merged, ...) and then prints the same snapshot with `event` set. It never
changes the pull request: every push, comment and merge stays an explicit command
of the agent. All state lives in the PR itself, so a new session can run it again.

Standard library only, no Node.js. Needs `gh` on PATH and Python 3.11 or newer.

Exit codes: 0 when a snapshot was printed, 2 when the arguments were wrong, 1 when the
run failed. The Node version split 2 from 1 by how the message began, so `--timeout soon`
exited 1 there and exits 2 here; stdout carries the same {"event": "error", "hint": ...}
either way, which is what the skill reads.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import quote


def _force_utf8_stdio():
    """Write UTF-8 with LF whatever the console code page says.

    The Node version got this for free: node always writes UTF-8. Python writes the
    platform encoding, so on a Windows console (cp949, cp1252) the Korean in `hint`
    came out as mojibake, and on a code page that cannot represent it at all the
    write raises UnicodeEncodeError and the watcher dies instead of reporting. The
    newline matters too: the agent and `jq` read LF, and text mode would turn every
    one into CRLF. This runs before anything is written, including the error paths.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except (AttributeError, OSError, ValueError):
            pass  # a replaced or non-text stream (a test harness, a pipe already wrapped)


_force_utf8_stdio()

# ---------------------------------------------------------------- python floor

# The one place the minimum version is written. The workflow and the README are
# checked against it by tests, so no second copy can drift from it.
MIN_PYTHON = (3, 11)
_PROBE_TIMEOUT_S = 3
_LOOKAHEAD = 3  # how many minor versions above the floor to look for
_REEXEC = "PR_MERGE_WATCH_REEXEC"
# The exec that replaces this process. A module attribute so the tests can swap it
# for one that refuses: a handover test that forgot to inject `execute` would
# otherwise turn the test runner itself into the watcher.
_EXEC = os.execvpe
_PROBE_CODE = "import sys;sys.stdout.write('%d.%d' % sys.version_info[:2])"


def floor_str() -> str:
    return "%d.%d" % MIN_PYTHON


def version_str(info=None) -> str:
    return "%d.%d" % tuple((info or sys.version_info)[:2])


def interpreter_candidates():
    """Interpreters to try, newest first, derived from the floor.

    `py -3` is the Windows launcher, which knows about installs not on PATH.
    """
    major, minor = MIN_PYTHON
    out = [["python%d.%d" % (major, m)] for m in range(minor + _LOOKAHEAD, minor - 1, -1)]
    out.append(["python%d" % major])
    out.append(["python"])
    out.append(["py", "-%d" % major])
    return out


def _resolve(candidate):
    """PATHEXT-aware resolution, the same `gh` gets.

    On Windows there are no python3.11..python3.14 executables, so the usable
    candidates are python, python3 and py - and pyenv-win installs those as .bat
    shims, which CreateProcess cannot find by bare name. Without this, a machine
    with 3.13 on PATH is told to go install Python.
    """
    return [shutil.which(candidate[0]) or candidate[0]] + list(candidate[1:])


def _probe(candidate):
    """The version `candidate` starts, or None if it will not run."""
    argv = _resolve(candidate)
    try:
        r = subprocess.run(
            argv + ["-c", _PROBE_CODE],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        major, minor = r.stdout.decode("ascii", "replace").strip().split(".")[:2]
        return (int(major), int(minor))
    except (ValueError, IndexError):
        return None


def upgrade_hint() -> str:
    """One command that gets a Python meeting the floor.

    The version comes from MIN_PYTHON, never a second hardcoded number. This prints
    the command rather than running it: installing a runtime changes the machine and
    usually needs an administrator, so it stays the user's call.
    """
    want = floor_str()
    if sys.platform == "win32":
        return "winget install Python.Python." + want
    if sys.platform == "darwin":
        return "brew install python@" + want
    for manager, command in (
        ("apt-get", "sudo apt install python" + want),
        ("dnf", "sudo dnf install python" + want),
        ("pacman", "sudo pacman -S python"),
        ("zypper", "sudo zypper install python" + want.replace(".", "")),
    ):
        if shutil.which(manager):
            return command
    return "install Python " + want + " or newer"


def shortfall_hint(found=None) -> str:
    return (
        "Python %s 이상이 필요하지만 이 머신에서 가장 새 것은 %s 입니다. 설치하면 바로 됩니다 — %s"
        % (floor_str(), version_str(found), upgrade_hint())
    )


def ensure_floor(argv, running=None, probe=None, spawn=None, execute=None, windows=None):
    """Make sure the interpreter running this meets the floor.

    Returns None to go ahead. Otherwise a newer interpreter on this machine runs the
    same command, or ("hint", text) says what to install. The caller reports that
    through the same JSON the skill already reads: a bare message on stderr would leave
    it with no `event` to act on.

    On POSIX the newer interpreter replaces this process (exec), so there is one
    process to signal or kill. Starting it as a child instead left the child running
    when only the parent was signalled: it was re-parented to init and, with the
    default --timeout 0, kept polling GitHub on the user's quota. Windows has no exec
    that keeps the caller's handle on the process - os.exec* there starts a new
    process and exits this one, so the caller would stop reading before the answer -
    so Windows still waits on a child and returns ("exit", code). That leaves the
    old gap there: terminating only the parent leaves the child polling. Closing it
    would take a Job Object, which the standard library does not offer.
    """
    running = tuple(running or sys.version_info[:2])
    if running >= MIN_PYTHON:
        return None
    if os.environ.get(_REEXEC):
        # A newer interpreter was already started once; a second hop would loop.
        return ("hint", shortfall_hint(running))
    probe = probe or _probe
    spawn = spawn or subprocess.run
    execute = execute or _EXEC
    windows = (sys.platform == "win32") if windows is None else windows
    best = running
    for candidate in interpreter_candidates():
        got = probe(candidate)
        if got is None:
            continue
        if got >= MIN_PYTHON:
            env = dict(os.environ)
            env[_REEXEC] = "1"
            cmd = _resolve(candidate) + [os.path.abspath(__file__)] + list(argv)
            if not windows:
                # Nothing has been written yet, but a buffered byte would be lost.
                sys.stdout.flush()
                sys.stderr.flush()
                execute(cmd[0], cmd, env)  # does not return; a failure raises OSError
                raise OSError("exec of %s returned without replacing this process" % cmd[0])
            r = spawn(cmd, env=env)
            code = r.returncode
            # A POSIX child killed by a signal reports -N; an exit status has to be
            # 0-255, so pass on the shell's convention instead of a negative number.
            return ("exit", 128 - code if code < 0 else code)
        best = max(best, got)
    return ("hint", shortfall_hint(best))


MARKER_RE = re.compile(r"<!--\s*pr-review-loop\s+([^>]*?)\s*-->")
# gh merges at once instead of arming auto-merge when the PR is in one of these states.
IMMEDIATELY_MERGEABLE = {"CLEAN", "HAS_HOOKS", "UNSTABLE"}
BODY_LIMIT = 3000


def _min_interval() -> float:
    """Polling faster than this only burns API quota; the tests lower it to keep themselves short."""
    try:
        v = float(os.environ.get("PR_MERGE_WATCH_MIN_INTERVAL") or 0)
    except ValueError:
        v = 0.0
    return v or 15.0


MIN_INTERVAL = _min_interval()

QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  viewer{login}
  repository(owner:$owner,name:$name){
    nameWithOwner autoMergeAllowed mergeCommitAllowed squashMergeAllowed rebaseMergeAllowed viewerDefaultMergeMethod
    pullRequest(number:$number){
      number url title state isDraft mergeable mergeStateStatus reviewDecision
      headRefOid headRefName baseRefName isInMergeQueue mergedAt
      mergeCommit{oid}
      author{login}
      autoMergeRequest{enabledAt mergeMethod enabledBy{login}}
      baseRef{branchProtectionRule{requiredApprovingReviewCount requiresStatusChecks requiredStatusCheckContexts requiresStrictStatusChecks requiresConversationResolution dismissesStaleReviews}}
      reviewRequests(first:30){nodes{requestedReviewer{__typename ... on User{login} ... on Bot{login} ... on Mannequin{login} ... on Team{slug}}}}
      latestOpinionatedReviews(first:50){nodes{author{__typename login} state commit{oid}}}
      latestReviews(first:50){nodes{author{login} state}}
      timelineItems(last:10,itemTypes:[AUTO_MERGE_ENABLED_EVENT,AUTO_SQUASH_ENABLED_EVENT,AUTO_REBASE_ENABLED_EVENT,AUTO_MERGE_DISABLED_EVENT]){nodes{
        __typename
        ... on AutoMergeEnabledEvent{actor{login}}
        ... on AutoSquashEnabledEvent{actor{login}}
        ... on AutoRebaseEnabledEvent{actor{login}}
        ... on AutoMergeDisabledEvent{actor{login} reasonCode}
      }}
      reviews(last:100){nodes{id url state body submittedAt author{__typename login}}}
      reviewThreads(last:100){nodes{id isResolved isOutdated path line comments(last:6){nodes{url body createdAt publishedAt author{__typename login}}}}}
      comments(last:100){pageInfo{hasPreviousPage startCursor} nodes{id url body createdAt author{__typename login}}}
      commits(last:1){nodes{commit{oid statusCheckRollup{state contexts(first:100){pageInfo{hasNextPage endCursor} nodes{...Ctx}}}}}}
    }
  }
}
fragment Ctx on StatusCheckRollupContext{
  __typename
  ... on CheckRun{name status conclusion detailsUrl startedAt isRequired(pullRequestNumber:$number) checkSuite{workflowRun{databaseId event workflow{name}}}}
  ... on StatusContext{context state targetUrl createdAt isRequired(pullRequestNumber:$number)}
}"""

MORE_CHECKS_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$after:String!){
  repository(owner:$owner,name:$name){pullRequest(number:$number){
    commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{...Ctx}}}}}}
  }}
}
fragment Ctx on StatusCheckRollupContext{
  __typename
  ... on CheckRun{name status conclusion detailsUrl startedAt isRequired(pullRequestNumber:$number) checkSuite{workflowRun{databaseId event workflow{name}}}}
  ... on StatusContext{context state targetUrl createdAt isRequired(pullRequestNumber:$number)}
}"""

OLDER_COMMENTS_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$before:String!){
  repository(owner:$owner,name:$name){pullRequest(number:$number){
    comments(last:100,before:$before){pageInfo{hasPreviousPage startCursor} nodes{id url body createdAt author{__typename login}}}
  }}
}"""

# ---------------------------------------------------------------- pure logic

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    # The time is optional: Date.parse accepts a bare date, and a `until=2026-11-13`
    # typed by hand into a marker must not read as 0 - that would move the anchor back
    # to the beginning of time and reopen every review item already answered.
    r"(?:[Tt ](\d{2}):(\d{2})"
    r"(?::(\d{2})(?:\.(\d+))?)?)?"
    r"(Z|z|[+-]\d{2}:?\d{2})?$"
)


def ts(value) -> int:
    """Epoch milliseconds for an ISO timestamp; 0 when absent or unparseable.

    Stands in for JavaScript's Date.parse. Every timestamp GitHub returns carries an
    offset; one without is read as UTC rather than as the machine's local zone, so two
    machines in different zones read the same PR the same way.
    """
    if not value:
        return 0
    m = _ISO_RE.match(str(value).strip())
    if not m:
        return 0
    year, month, day = (int(m.group(i)) for i in range(1, 4))
    hour = int(m.group(4) or 0)
    minute = int(m.group(5) or 0)
    second = int(m.group(6) or 0)
    frac = m.group(7) or ""
    micro = int((frac + "000000")[:6]) if frac else 0
    try:
        dt = datetime.datetime(
            year, month, day, hour, minute, second, micro, tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return 0
    offset_minutes = 0
    zone = m.group(8)
    if zone and zone not in ("Z", "z"):
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        offset_minutes = sign * (int(digits[:2]) * 60 + int(digits[2:4]))
    return int(dt.timestamp() * 1000) - offset_minutes * 60_000


def _iso(ms: int, keep_millis: bool) -> str:
    dt = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    out = "%s.%03dZ" % (dt.strftime("%Y-%m-%dT%H:%M:%S"), dt.microsecond // 1000)
    return out if keep_millis else out.replace(".000Z", "Z")


def now_iso() -> str:
    return _iso(int(time.time() * 1000), True)


def trim(s) -> str:
    """Shorten a body to BODY_LIMIT, telling the reader where the rest is.

    One deliberate difference from the Node version: it measured JavaScript's
    .length, which counts UTF-16 units, so an emoji cost two. This counts code
    points. Only the cut point of an over-long body moves, and reproducing the
    old count faithfully would mean slicing surrogate pairs in half.
    """
    t = (s or "").strip()
    if len(t) > BODY_LIMIT:
        return t[:BODY_LIMIT] + "\n…(%d more characters, open the url)" % (len(t) - BODY_LIMIT)
    return t


def parse_marker(body):
    """Parse the `<!-- pr-review-loop step=.. round=.. sha=.. until=.. -->` marker of a comment."""
    m = MARKER_RE.search(body or "")
    if not m:
        return None
    out = {}
    # Lists are written `a,b`; a hand-typed `a, b` or a quoted value must not cut the list short.
    inner = re.sub(r"\s*,\s*(?!(?:step|round|sha|until|mode|ask)=)", ",", m.group(1))
    for pair in re.split(r"\s+", inner):
        i = pair.find("=")
        if i > 0:
            out[pair[:i]] = re.sub(r"^[\"']|[\"']$", "", pair[i + 1:])
    return out if out.get("step") else None


def normalize_checks(nodes):
    """Map raw rollup contexts to one entry per check (workflow, event, name).

    A head commit often carries several runs of the same check: re-runs, and runs that label or
    comment events add and that end SKIPPED or CANCELLED without testing anything. The entry is the
    latest run that actually decided something (passed, failed, or still running); only when no
    such run exists does a skipped or cancelled one speak for the check.
    """
    inconclusive = {"SKIPPED", "NEUTRAL", "CANCELLED", "STALE"}
    by_key = {}
    for n in nodes or []:
        kind = n.get("__typename")
        if kind == "CheckRun":
            work = ((n.get("checkSuite") or {}).get("workflowRun")) or {}
            workflow = (work.get("workflow") or {}).get("name") or ""
            done = n.get("status") == "COMPLETED"
            conclusion = n.get("conclusion")
            if not done:
                state = "pending"
            elif conclusion == "SUCCESS":
                state = "pass"
            elif conclusion in ("NEUTRAL", "SKIPPED"):
                state = "skip"
            else:
                state = "fail"
            check = {
                "key": "%s|%s|%s" % (workflow, work.get("event") or "", n.get("name")),
                "name": n.get("name"),
                "workflow": workflow or None,
                "state": state,
                "conclusion": conclusion if done else n.get("status"),
                "decided": not (done and conclusion in inconclusive),
                "required": bool(n.get("isRequired")),
                "runId": work.get("databaseId") or None,
                "url": n.get("detailsUrl") or None,
                "at": ts(n.get("startedAt")),
            }
        elif kind == "StatusContext":
            raw = n.get("state")
            state = "pass" if raw == "SUCCESS" else ("pending" if raw in ("PENDING", "EXPECTED") else "fail")
            check = {
                "key": "status|%s" % n.get("context"),
                "name": n.get("context"),
                "workflow": None,
                "state": state,
                "conclusion": raw,
                "decided": True,
                "required": bool(n.get("isRequired")),
                "runId": None,
                "url": n.get("targetUrl") or None,
                "at": ts(n.get("createdAt")),
            }
        else:
            continue
        by_key.setdefault(check["key"], []).append(check)

    out = []
    for runs in by_key.values():
        decided = [c for c in runs if c["decided"]]
        pool = decided or runs
        latest = pool[0]
        for c in pool[1:]:
            if c["at"] >= latest["at"]:
                latest = c
        out.append({k: v for k, v in latest.items() if k != "decided"})
    return out


def summarize_checks(checks, required_contexts=None, skip_ok=False):
    """Decide which checks gate the merge.

    Required checks gate it when the repository marks any; with no required checks at all,
    every check counts (nothing tells them apart).
    """
    required_contexts = list(required_contexts or [])

    def required(c):
        return c["required"] or c["name"] in required_contexts

    any_required = any(required(c) for c in checks) or len(required_contexts) > 0

    def gates(c):
        return (not any_required) or required(c)

    def view(c):
        return {
            "name": c["name"], "workflow": c["workflow"], "conclusion": c["conclusion"],
            "runId": c["runId"], "url": c["url"],
        }

    def skip_intended(name):
        return skip_ok is True or (isinstance(skip_ok, (list, tuple, set)) and name in skip_ok)

    gating = [c for c in checks if gates(c)]
    required_names = []
    for c in checks:
        if required(c) and c["name"] not in required_names:
            required_names.append(c["name"])

    return {
        "gating": "required" if any_required else "all",
        "total": len(checks),
        "passed": len([c for c in gating if c["state"] in ("pass", "skip")]),
        "pending": [c["name"] for c in gating if c["state"] == "pending"],
        "failed": [view(c) for c in gating if c["state"] == "fail"],
        # Required by the branch rules but not reported on this commit yet (e.g. CI that needs a label or a manual start).
        "missingRequired": [n for n in required_contexts if not any(c["name"] == n for c in checks)],
        # Required, and every run of it on this commit was SKIPPED. GitHub counts that as passed and will
        # merge on it, but the check never ran (label-gated CI after a push). --skip-ok accepts the skips
        # a repository intends, for all checks or for the named ones.
        "skipped": [
            name for name in required_names
            if all(c["conclusion"] == "SKIPPED" for c in checks if c["name"] == name)
            and not skip_intended(name)
        ],
        "optionalFailed": [view(c) for c in checks if not gates(c) and c["state"] == "fail"],
    }


def collect_feedback(pr, viewer):
    """Reviewer feedback that still needs the agent.

    - A review thread is open while it is unresolved and its last comment is not ours.
    - A review body or a conversation comment is open while it is newer than the `until`
      of our last `step=feedback` marker comment.
    - Whatever a person wrote before their own latest approval is settled. Comments submitted
      with the approval itself ("approve with nits") carry its timestamp and stay open.
    - An item the agent means to decline is parked in `awaitingUser` instead: the reply needs the
      user's approval first. Our latest `step=feedback` marker lists those ids in `ask`; an item
      stays parked until we answer it, the reviewer adds to it, or a later marker drops the id.
    """
    pr_author = (pr.get("author") or {}).get("login")

    # "Ours" is the account running gh and the PR author (normally the same account).
    def ours(login):
        return login == viewer or login == pr_author

    # Only our own comments carry state: anyone can type a marker, and a forged `step=open`
    # would read as the user's acceptance.
    markers = []
    for c in (pr.get("comments") or {}).get("nodes") or []:
        login = (c.get("author") or {}).get("login")
        m = parse_marker(c.get("body")) if ours(login) else None
        if m:
            entry = dict(m)
            entry["at"] = c.get("createdAt")
            entry["url"] = c.get("url")
            markers.append(entry)

    recorded = [m for m in markers if m.get("step") == "feedback"]
    anchor = max([0] + [ts(m.get("until")) for m in recorded])
    ask = set()
    if recorded:
        raw = recorded[-1].get("ask") or ""
        ask = {i for i in (re.sub(r"[\"']", "", p) for p in raw.split(",")) if i}

    approved_at = {}
    for r in (pr.get("reviews") or {}).get("nodes") or []:
        if r.get("state") == "APPROVED":
            login = (r.get("author") or {}).get("login")
            approved_at[login] = max(approved_at.get(login, 0), ts(r.get("submittedAt")))

    def waived(login, at):
        return at < approved_at.get(login, 0)

    def who(a):
        a = a or {}
        out = {"author": a.get("login") or "ghost"}
        if a.get("__typename") == "Bot":
            out["bot"] = True
        return out

    items = []
    awaiting_user = []
    state = {"until": 0}

    # Open items wake the agent. An item already covered by a feedback marker is done, unless that
    # marker parked it for the user (`ask`).
    def sort(item, at):
        if at > anchor:
            items.append(item)
            state["until"] = max(state["until"], at)
        elif item["id"] in ask:
            awaiting_user.append(item)

    for r in (pr.get("reviews") or {}).get("nodes") or []:
        author = r.get("author") or {}
        at = ts(r.get("submittedAt"))
        if ours(author.get("login")) or r.get("state") in ("PENDING", "DISMISSED"):
            continue
        if r.get("state") != "CHANGES_REQUESTED" and not (r.get("body") or "").strip():
            continue
        if r.get("state") != "APPROVED" and waived(author.get("login"), at):
            continue
        item = {"kind": "review", "id": r.get("id")}
        item.update(who(author))
        item["state"] = r.get("state")
        item["url"] = r.get("url")
        item["body"] = trim(r.get("body"))
        sort(item, at)

    for t in (pr.get("reviewThreads") or {}).get("nodes") or []:
        comments = (t.get("comments") or {}).get("nodes") or []
        last = comments[-1] if comments else None
        last_author = (last or {}).get("author") or {}
        if t.get("isResolved") or not last or ours(last_author.get("login")):
            continue
        at = ts(last.get("publishedAt") or last.get("createdAt"))
        if waived(last_author.get("login"), at):
            continue
        item = {"kind": "thread", "id": t.get("id")}
        item.update(who(last_author))
        item["threadId"] = t.get("id")
        item["path"] = t.get("path")
        item["line"] = t.get("line")
        if t.get("isOutdated"):
            item["outdated"] = True
        item["url"] = last.get("url")
        item["comments"] = [
            {"author": (c.get("author") or {}).get("login") or "ghost", "body": trim(c.get("body"))}
            for c in comments
        ]
        # A thread stays open until we answer it, whatever the marker says, unless it is parked.
        if item["id"] in ask and at <= anchor:
            awaiting_user.append(item)
        else:
            items.append(item)
            state["until"] = max(state["until"], at)

    for c in (pr.get("comments") or {}).get("nodes") or []:
        author = c.get("author") or {}
        at = ts(c.get("createdAt"))
        if ours(author.get("login")) or author.get("__typename") == "Bot":
            continue
        if waived(author.get("login"), at):
            continue
        item = {"kind": "comment", "id": c.get("id")}
        item.update(who(author))
        item["url"] = c.get("url")
        item["body"] = trim(c.get("body"))
        sort(item, at)

    return {
        "until": _iso(state["until"], False) if state["until"] else None,
        "items": items,
        "awaitingUser": awaiting_user,
        "markers": markers,
    }


def summarize_markers(markers):
    """Where the procedure stands, read from our marker comments.

    - `step=open` records the user's acceptance (its sha is the accepted commit) and the merge mode.
    - `step=hold` records a question waiting for the user; the next `step=open` closes it.
    """
    last = markers[-1] if markers else None
    gate, gate_at = None, -1
    opened = None
    for i, m in enumerate(markers):
        if m.get("step") in ("open", "hold"):
            gate, gate_at = m, i
        if m.get("step") == "open":
            opened = m

    def count(step):
        return len([m for m in markers if m.get("step") == step])

    last_view = None
    if last:
        last_view = {"step": last.get("step")}
        # JSON.stringify drops an absent round or sha; a marker typed without them reads the same here.
        for key in ("round", "sha"):
            if last.get(key) is not None:
                last_view[key] = last.get(key)

    mode = None
    if opened:
        mode = opened.get("mode") if opened.get("mode") in ("manual", "unreviewed") else "auto"

    return {
        "last": last_view,
        "acceptedSha": (opened or {}).get("sha") or None,
        # auto: GitHub merges. manual: the agent merges after a person approved the head and checks passed.
        # unreviewed: the user chose to merge without a review; the agent merges once checks passed.
        "mode": mode,
        "hold": gate.get("url") if gate and gate.get("step") == "hold" else None,
        # The hold asked whether to re-evaluate and the review loop has run since (its CI comments may come last).
        "reevaluating": bool(
            gate and gate.get("step") == "hold"
            and any(m.get("step") in ("review", "fix", "poc") for m in markers[gate_at + 1:])
        ),
        "next": {
            "open": count("open") + 1, "hold": count("hold") + 1,
            "ci": count("ci") + 1, "feedback": count("feedback") + 1,
        },
    }


def merge_rules(classic, rulesets):
    """Merge classic branch protection (GraphQL) and rulesets (REST) into one view of the base branch rules."""
    out = {
        "requiredContexts": [], "requiredApprovals": 0, "dismissStaleOnPush": False,
        "threadResolutionRequired": False, "upToDateRequired": False, "mergeQueue": False,
        "allowedMergeMethods": None,
    }
    if classic:
        if classic.get("requiresStatusChecks"):
            out["requiredContexts"].extend(classic.get("requiredStatusCheckContexts") or [])
        out["requiredApprovals"] = classic.get("requiredApprovingReviewCount") or 0
        out["dismissStaleOnPush"] = bool(classic.get("dismissesStaleReviews"))
        out["threadResolutionRequired"] = bool(classic.get("requiresConversationResolution"))
        out["upToDateRequired"] = bool(classic.get("requiresStrictStatusChecks"))

    for rule in rulesets or []:
        params = rule.get("parameters") or {}
        kind = rule.get("type")
        if kind == "required_status_checks":
            out["requiredContexts"].extend(
                c.get("context") for c in (params.get("required_status_checks") or [])
            )
            out["upToDateRequired"] = out["upToDateRequired"] or bool(
                params.get("strict_required_status_checks_policy")
            )
        elif kind == "pull_request":
            out["requiredApprovals"] = max(
                out["requiredApprovals"], params.get("required_approving_review_count") or 0
            )
            out["dismissStaleOnPush"] = out["dismissStaleOnPush"] or bool(
                params.get("dismiss_stale_reviews_on_push")
            )
            out["threadResolutionRequired"] = out["threadResolutionRequired"] or bool(
                params.get("required_review_thread_resolution")
            )
            allowed = params.get("allowed_merge_methods")
            if isinstance(allowed, list):
                current = out["allowedMergeMethods"]
                if current is None:
                    current = allowed
                out["allowedMergeMethods"] = [m for m in current if m in allowed]
        elif kind == "merge_queue":
            out["mergeQueue"] = True

    seen = []
    for name in out["requiredContexts"]:
        if name not in seen:
            seen.append(name)
    out["requiredContexts"] = seen
    return out


# GitHub's own view of the review requirement: satisfied, or nothing required and nobody objecting.
def _review_ok(s):
    review = s["review"]
    return review["decision"] == "APPROVED" or (
        review["decision"] is None and not review["changesRequested"]
    )


# What a merge by the agent itself needs: a person approved this very commit and nobody objects.
def _person_approved(s):
    return _review_ok(s) and len(s["review"]["approvals"]) > 0


def _checks_ok(s):
    c = s["checks"]
    return not c["failed"] and not c["pending"] and not c["missingRequired"] and not c["skipped"]


# Effects under which an armed auto-merge would merge without the review or the CI the procedure promises.
UNSAFE_EFFECTS = {"no_review", "not_enforced", "ci_not_run", "ci_not_green"}


def auto_merge_effect(s):
    """What auto-merge would do with this PR.

    It waits only for what the base branch requires, and `gh pr merge --auto` merges at once when
    nothing blocks the PR. So "enable auto-merge" can mean "merge unreviewed" or "merge without
    waiting for CI"; those cases get their own answer, also when auto-merge is already on.
    """
    if s["pr"]["state"] != "OPEN":
        return "closed"
    if s["pr"]["isDraft"]:
        return "draft"
    if not s["autoMerge"]["allowed"]:
        return "not_allowed"
    if s["pr"]["mergeStateStatus"] == "UNKNOWN" and not s["autoMerge"]["enabled"]:
        return "unknown"
    # A required check that only ever skipped counts as passed for GitHub: nothing would wait for CI to run.
    if s["checks"]["skipped"]:
        return "ci_not_run"
    mergeable = s["pr"]["mergeStateStatus"] in IMMEDIATELY_MERGEABLE
    now = mergeable and _checks_ok(s)
    if s["review"]["decision"] is None:
        # No review is required, so auto-merge never waits for one: not now, and not after a later push.
        if not s["review"]["approvals"]:
            return "no_review"
        return "merges_now" if now else "not_enforced"
    # CI exists but none of it is required: auto-merge merges on approval, whatever the checks say.
    if s["checks"]["gating"] == "all" and s["checks"]["total"] > 0:
        return "merges_now" if now else "not_enforced"
    # From here the base branch requires both a review and checks. GitHub can still call the PR
    # mergeable over a check that ran and did not pass (a later skipped run covers an earlier failure).
    if mergeable and not now:
        return "ci_not_green"
    if s["autoMerge"]["enabled"]:
        return "enabled"
    return "merges_now" if mergeable else "arms"


def classify(s, expect_auto=False, no_auto=False, no_review=False):
    """Events that are true the moment the snapshot is taken, most urgent first."""
    if s["pr"]["state"] == "MERGED":
        return ["merged"]
    if s["pr"]["state"] == "CLOSED":
        return ["closed"]
    if s["pr"]["isDraft"]:
        return ["draft"]
    events = []
    # A question to the user is open: nothing may merge, and auto-merge is off on purpose.
    hold = bool(s["markers"]["hold"])
    if hold:
        events.append("hold")
    if s["pr"]["mergeable"] == "CONFLICTING":
        events.append("conflict")
    if s["checks"]["failed"]:
        events.append("ci_failed")
    if s["feedback"]["items"]:
        events.append("feedback")
    if s["pr"]["mergeStateStatus"] == "BEHIND":
        events.append("behind")
    if expect_auto and not hold and not s["autoMerge"]["enabled"]:
        events.append("auto_merge_off")
    # Auto-merge is on but would not wait for the review or the CI (e.g. the approval went stale with a push).
    if (
        expect_auto and not hold and s["autoMerge"]["enabled"]
        and s["autoMerge"]["effect"] in UNSAFE_EFFECTS
    ):
        events.append("auto_merge_unsafe")
    # Without auto-merge the agent is the only gate: a person approved this head (unless the user
    # waived the review), every check passed, and GitHub calls the PR mergeable.
    reviewed = (not s["review"]["changesRequested"]) if no_review else _person_approved(s)
    if (
        no_auto and not hold and reviewed and _checks_ok(s)
        and s["pr"]["mergeStateStatus"] in IMMEDIATELY_MERGEABLE
    ):
        events.append("ready")
    return events


def timed_events(s, opts, since, now, grace_ms):
    """Events that only count once a condition has held for `grace_ms` (wait mode).

    `since` carries the first time each condition was seen across polls.
    """
    no_auto = (opts or {}).get("no_auto", False)

    def held(name, active):
        if not active:
            since.pop(name, None)
            return False
        since.setdefault(name, now)
        return now - since[name] >= grace_ms

    open_pr = s["pr"]["state"] == "OPEN" and not s["pr"]["isDraft"]
    events = []
    if held(
        "ci_not_started",
        open_pr and (len(s["checks"]["missingRequired"]) > 0 or len(s["checks"]["skipped"]) > 0),
    ):
        events.append("ci_not_started")
    # Everything we can see is green and auto-merge is on, yet GitHub has not merged (a merge queue is a normal wait).
    # A thread that must be resolved and waits for the user's approval of the reply explains the wait.
    parked = s["rules"]["threadResolutionRequired"] and any(
        i.get("kind") == "thread" for i in s["feedback"]["awaitingUser"]
    )
    if held(
        "stuck",
        open_pr and not no_auto and s["autoMerge"]["enabled"] and not s["pr"].get("inMergeQueue")
        and _review_ok(s) and _checks_ok(s) and not parked,
    ):
        events.append("stuck")
    return events


def build_snapshot(data, rulesets=None, extra_contexts=None, skip_ok=False):
    """Build the snapshot the agent reads from the GraphQL payload and the base branch rules."""
    repo = data["repository"]
    pr = repo["pullRequest"]
    viewer = data["viewer"]["login"]
    commit_nodes = (pr.get("commits") or {}).get("nodes") or []
    commit = (commit_nodes[0] or {}).get("commit") if commit_nodes else None
    rules = merge_rules((pr.get("baseRef") or {}).get("branchProtectionRule"), rulesets)
    rollup_nodes = (((commit or {}).get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
    checks = summarize_checks(
        normalize_checks(list(rollup_nodes) + list(extra_contexts or [])),
        rules["requiredContexts"],
        skip_ok=skip_ok,
    )
    feedback = collect_feedback(pr, viewer)
    markers = feedback.pop("markers")
    threads = (pr.get("reviewThreads") or {}).get("nodes") or []
    opinions = (pr.get("latestOpinionatedReviews") or {}).get("nodes") or []
    approved = [
        r for r in opinions
        if r.get("state") == "APPROVED" and (r.get("author") or {}).get("__typename") != "Bot"
    ]
    methods = []
    for allowed, name in (
        (repo.get("mergeCommitAllowed"), "merge"),
        (repo.get("squashMergeAllowed"), "squash"),
        (repo.get("rebaseMergeAllowed"), "rebase"),
    ):
        # `is None`, not falsiness: an empty list means the rulesets left no method
        # allowed, and JS read `[]` as truthy, so it excluded everything. Treating it
        # as "no restriction" would offer gh a method the branch rules forbid.
        if allowed and (
            rules["allowedMergeMethods"] is None or name in rules["allowedMergeMethods"]
        ):
            methods.append(name)
    preferred = (repo.get("viewerDefaultMergeMethod") or "").lower()
    # The last auto-merge event tells a merge that was never armed from one somebody switched off.
    auto_events = (pr.get("timelineItems") or {}).get("nodes") or []
    last_auto = auto_events[-1] if auto_events else None

    pr_view = {
        "number": pr.get("number"), "url": pr.get("url"), "title": pr.get("title"),
        "state": pr.get("state"), "isDraft": pr.get("isDraft"),
        "base": pr.get("baseRefName"), "head": pr.get("headRefName"), "headSha": pr.get("headRefOid"),
        "mergeable": pr.get("mergeable"), "mergeStateStatus": pr.get("mergeStateStatus"),
    }
    if pr.get("isInMergeQueue"):
        pr_view["inMergeQueue"] = True
    if pr.get("mergedAt"):
        pr_view["mergedAt"] = pr.get("mergedAt")
        pr_view["mergeCommit"] = (pr.get("mergeCommit") or {}).get("oid") or None

    auto_merge = {
        "allowed": bool(repo.get("autoMergeAllowed")),
        "enabled": bool(pr.get("autoMergeRequest")),
    }
    request = pr.get("autoMergeRequest")
    if request:
        auto_merge["method"] = (request.get("mergeMethod") or "").lower()
        auto_merge["enabledBy"] = (request.get("enabledBy") or {}).get("login")
    elif last_auto and last_auto.get("__typename") == "AutoMergeDisabledEvent":
        # Set when auto-merge was on and somebody (or GitHub) switched it off; absent when it was never armed.
        auto_merge["disabledBy"] = (last_auto.get("actor") or {}).get("login") or "github"

    s = {
        "event": None,
        "at": now_iso(),
        "pr": pr_view,
        "repo": {
            "nameWithOwner": repo.get("nameWithOwner"),
            "mergeMethods": methods,
            "defaultMergeMethod": preferred if preferred in methods else (methods[0] if methods else None),
        },
        "autoMerge": auto_merge,
        "rules": rules,
        "review": {
            "decision": pr.get("reviewDecision") or None,
            # People who approved the current head. `decision` is GitHub's verdict under the branch rules;
            # this list is what a merge without auto-merge leans on, so bots and older commits do not count.
            "approvals": [
                (r.get("author") or {}).get("login") for r in approved
                if (r.get("commit") or {}).get("oid") == pr.get("headRefOid")
            ],
            "staleApprovals": [
                (r.get("author") or {}).get("login") for r in approved
                if (r.get("commit") or {}).get("oid") != pr.get("headRefOid")
            ],
            "changesRequested": [
                (r.get("author") or {}).get("login") for r in opinions
                if r.get("state") == "CHANGES_REQUESTED"
            ],
            # Reviewers whose last review was dismissed (e.g. an approval made stale by a push): they need to look again.
            "dismissed": [
                (r.get("author") or {}).get("login")
                for r in ((pr.get("latestReviews") or {}).get("nodes") or [])
                if r.get("state") == "DISMISSED"
            ],
            "requested": [
                login for login in (
                    (n.get("requestedReviewer") or {}).get("login")
                    or (n.get("requestedReviewer") or {}).get("slug")
                    for n in ((pr.get("reviewRequests") or {}).get("nodes") or [])
                ) if login
            ],
            "unresolvedThreads": len([t for t in threads if not t.get("isResolved")]),
        },
        "checks": checks,
        "feedback": feedback,
        "markers": summarize_markers(markers),
    }
    s["autoMerge"]["effect"] = auto_merge_effect(s)

    now = s["pr"]["mergeStateStatus"] in IMMEDIATELY_MERGEABLE
    if s["checks"]["failed"]:
        ci = "실패한 체크: %s" % ", ".join(c["name"] for c in s["checks"]["failed"])
    elif s["checks"]["pending"]:
        ci = "진행 중인 체크: %s" % ", ".join(s["checks"]["pending"])
    elif s["checks"]["total"]:
        ci = "체크는 모두 통과"
    else:
        ci = "체크 없음"

    effect = s["autoMerge"]["effect"]
    note = None
    if effect == "no_review":
        note = (
            "base 가 리뷰를 요구하지 않고 이 커밋을 승인한 사람도 없다. gh pr merge --auto 는 %s "
            "리뷰 없이 머지한다. 지금 %s."
            % ("기다리지 않고 바로" if now else "필수 체크만 통과하면", ci)
        )
    elif effect == "not_enforced":
        note = (
            "base 가 리뷰를 요구하지 않는다. auto-merge 는 지금의 승인이 푸시로 낡아도 다시 기다리지 않는다."
            if s["review"]["decision"] is None
            else "필수로 지정된 체크가 없다. auto-merge 는 CI 를 기다리지 않고 승인만으로 머지한다."
        )
    elif effect == "ci_not_run":
        note = (
            "필수 체크가 실행되지 않고 skip 됐다: %s. GitHub 는 통과로 치므로 CI 를 먼저 돌린다."
            % ", ".join(s["checks"]["skipped"])
        )
    elif effect == "ci_not_green":
        note = (
            "GitHub 는 머지 가능으로 보지만 통과하지 못한 체크가 있다 (%s). "
            "gh pr merge --auto 는 그대로 머지하므로 CI 를 먼저 통과시킨다." % ci
        )
    elif effect == "merges_now":
        note = "승인과 체크가 이미 찼다. gh pr merge --auto 는 바로 머지한다."
    elif effect == "not_allowed":
        note = "저장소 설정에서 auto-merge 가 꺼져 있다."
    if note:
        s["autoMerge"]["note"] = note
    return s


def hint(s, no_auto=False, no_review=False):
    """One line (Korean, for the skill's reader) saying why the watcher woke the agent or what it waits for."""
    def names(xs):
        xs = list(xs)
        return ", ".join(xs[:5]) + (" 외 %d개" % (len(xs) - 5) if len(xs) > 5 else "")

    event = s.get("event")
    awaiting = len(s["feedback"]["awaitingUser"])
    parked_note = (
        " 올리지 않은 거절 답글 %d건이 남아 있다(승인 대기 또는 답하지 않기로 함)." % awaiting
        if awaiting else ""
    )

    if event == "merged":
        commit = s["pr"].get("mergeCommit")
        return "머지됨 (%s). 절차를 끝낸다.%s" % (
            commit[:12] if commit else "merge commit 미확인", parked_note
        )
    if event == "closed":
        return "머지 없이 닫혔다. 사용자에게 보고한다.%s" % parked_note
    if event == "hold":
        return "사용자 결정을 기다리는 보류가 열려 있다 (%s). 그 질문의 답부터 받는다.%s" % (
            s["markers"]["hold"],
            " auto-merge 가 켜져 있다: 먼저 끈다(gh pr merge --disable-auto)." if s["autoMerge"]["enabled"] else "",
        )
    if event == "draft":
        return "아직 draft 다. 사용자가 채택한 뒤 gh pr ready 로 연다."
    if event == "conflict":
        return "base(%s)와 충돌한다. base 를 merge 해 충돌을 푼다." % s["pr"]["base"]
    if event == "ci_failed":
        return "머지를 막는 체크 실패: %s" % names(c["name"] for c in s["checks"]["failed"])
    if event == "feedback":
        return "리뷰어 코멘트 %d건이 답을 기다린다." % len(s["feedback"]["items"])
    if event == "behind":
        return "base(%s)보다 뒤처졌다. gh pr update-branch 로 맞춘다." % s["pr"]["base"]
    if event == "auto_merge_off":
        return (
            "auto-merge 를 %s 가 껐다. 다시 켜지 않고 사용자에게 보고한다." % s["autoMerge"]["disabledBy"]
            if s["autoMerge"].get("disabledBy")
            else "auto-merge 가 켜진 적이 없다. auto-merge 켜기 단계부터 한다."
        )
    if event == "auto_merge_unsafe":
        return (
            "auto-merge 가 켜져 있지만 기다려 주지 않는 조건이 있다: %s 먼저 끈다(gh pr merge --disable-auto)."
            % s["autoMerge"].get("note")
        )
    if event == "ready":
        return "%s 모두 찼다. auto-merge 없이 감시 중이므로 직접 머지할 차례다." % (
            "체크가" if no_review else "승인과 체크가"
        )
    if event == "ci_not_started":
        return "필수 체크가 돌지 않았다: %s. 레포의 CI 실행 규칙(라벨, 수동 실행)을 확인한다." % names(
            list(s["checks"]["missingRequired"]) + list(s["checks"]["skipped"])
        )
    if event == "stuck":
        return "승인과 보이는 체크가 모두 찼는데 머지되지 않는다 (mergeStateStatus=%s, 미해결 스레드 %d개)." % (
            s["pr"]["mergeStateStatus"], s["review"]["unresolvedThreads"]
        )
    if event in ("timeout", "waiting"):
        review = s["review"]
        parts = []
        if s["checks"]["pending"]:
            parts.append("체크 진행 중: %s" % names(s["checks"]["pending"]))
        if s["checks"]["missingRequired"]:
            parts.append("필수 체크 미보고: %s" % names(s["checks"]["missingRequired"]))
        if s["checks"]["skipped"]:
            parts.append("필수 체크가 실행되지 않음(skip): %s" % names(s["checks"]["skipped"]))
        if no_auto and not no_review and review["staleApprovals"] and not review["approvals"]:
            parts.append("이전 커밋의 승인만 있다: %s 의 재승인 대기" % names(review["staleApprovals"]))
        if review["decision"] == "REVIEW_REQUIRED":
            parts.append(
                "리뷰 승인 대기%s" % (
                    " (요청: %s)" % names(review["requested"]) if review["requested"]
                    else " (요청된 리뷰어 없음)"
                )
            )
        if review["decision"] == "CHANGES_REQUESTED":
            asked = all(r in review["requested"] for r in review["changesRequested"])
            parts.append(
                "변경 요청 상태: %s %s" % (
                    names(review["changesRequested"]),
                    "의 재리뷰 대기" if asked else "(재리뷰 요청 전)",
                )
            )
        if (
            no_auto and not no_review and review["decision"] != "REVIEW_REQUIRED"
            and not review["approvals"] and not review["staleApprovals"]
        ):
            parts.append("사람의 Approve 대기 (auto-merge 없이 승인 뒤 직접 머지)")
        if no_review and not parts:
            parts.append("머지 가능 상태 대기 (리뷰 없이, 체크 통과 뒤 직접 머지)")
        if awaiting:
            parts.append("거절 답글 %d건이 사용자 승인 대기(답하지 않기로 한 스레드 포함)" % awaiting)
        return " / ".join(parts) or (
            "기다릴 조건이 보이지 않는다 (mergeStateStatus=%s)." % s["pr"]["mergeStateStatus"]
        )
    return ""


# ---------------------------------------------------------------- gh access


class GhError(Exception):
    """Errors that waiting cannot cure (no gh, no login, no such PR) carry `fatal`; the rest are retried."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


class UsageError(Exception):
    pass


def gh(args):
    # which() honours PATHEXT, so a gh installed as gh.cmd or gh.bat is found; passing a
    # bare "gh" to CreateProcess only ever finds gh.exe. node's spawnSync looked at PATHEXT.
    exe = shutil.which("gh") or "gh"
    try:
        r = subprocess.run(
            [exe] + list(args), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except OSError as e:
        raise GhError("gh 를 실행할 수 없다: %s" % e, True)
    if r.returncode != 0 and not r.stdout:
        msg = (r.stderr or "").strip() or ("gh exited %s" % r.returncode)
        raise GhError(msg, bool(re.search(r"auth login|HTTP 401|Bad credentials", msg, re.I)))
    return r.stdout


def graphql(query, variables):
    args = ["api", "graphql", "-f", "query=%s" % query]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) and not isinstance(value, bool) else "-f"
        args += [flag, "%s=%s" % (key, value)]
    out = gh(args)
    try:
        res = json.loads(out)
    except ValueError:
        raise GhError("gh 출력이 JSON 이 아니다: %s" % out[:200], False)
    pull = ((res.get("data") or {}).get("repository") or {}).get("pullRequest")
    if not pull:
        errors = res.get("errors")
        # `is not None`, not falsiness: JS read an empty errors array as truthy, so a
        # response carrying `"errors": []` stayed a retryable blip. Reading it as "no
        # errors" here would fall through to the message branch and end the watch.
        if errors is not None:
            # The same fallback chain the original had: joined messages, then the
            # top-level message, then a default. Without it an `errors: []` answer, or
            # errors carrying no message, left the skill a `hint` of "".
            msg = (
                "; ".join(e.get("message") or "" for e in errors)
                or res.get("message")
                or "pull request not found"
            )
            gone = any(e.get("type") == "NOT_FOUND" for e in errors)
        else:
            msg = res.get("message") or "pull request not found"
            gone = not res.get("message")
        raise GhError(msg, gone or bool(re.search(r"Bad credentials", msg, re.I)))
    return res["data"]


def fetch_rulesets(repo, base):
    try:
        out = gh(["api", "repos/%s/rules/branches/%s" % (repo, quote(base, safe=""))])
    except GhError:
        return None  # could not ask (network): the caller goes on without them and asks again next time
    try:
        rules = json.loads(out)
    except ValueError:
        return []
    # an error object: not visible to this account, or an older GitHub Enterprise
    return rules if isinstance(rules, list) else []


def fetch_snapshot(target, cache):
    owner, name = target["repo"].split("/")
    variables = {"owner": owner, "name": name, "number": target["number"]}
    data = graphql(QUERY, variables)
    pr = data["repository"]["pullRequest"]

    extra = []
    nodes = (pr.get("commits") or {}).get("nodes") or []
    rollup = (((nodes[0] or {}).get("commit") or {}).get("statusCheckRollup") or {}) if nodes else {}
    page = (rollup.get("contexts") or {}).get("pageInfo")
    while page and page.get("hasNextPage"):
        more = graphql(MORE_CHECKS_QUERY, dict(variables, after=page["endCursor"]))
        contexts = (
            more["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]
            ["statusCheckRollup"]["contexts"]
        )
        extra.extend(contexts.get("nodes") or [])
        page = contexts.get("pageInfo")

    # The marker comments decide where the procedure stands, so a PR with more than 100
    # comments needs the older pages too. They do not change, so they are fetched once per window.
    first = (pr.get("comments") or {}).get("pageInfo")
    if first and first.get("hasPreviousPage"):
        if cache.get("olderBefore") != first.get("startCursor"):
            older = []
            info = first
            for _ in range(20):
                if not (info and info.get("hasPreviousPage")):
                    break
                more = graphql(
                    OLDER_COMMENTS_QUERY, dict(variables, before=info["startCursor"])
                )["repository"]["pullRequest"]["comments"]
                older = list(more.get("nodes") or []) + older
                info = more.get("pageInfo")
            cache["olderBefore"] = first.get("startCursor")
            cache["older"] = older
        pr["comments"]["nodes"] = list(cache.get("older") or []) + list(pr["comments"]["nodes"])

    if cache.get("base") != pr.get("baseRefName"):
        rulesets = fetch_rulesets(target["repo"], pr["baseRefName"])
        cache["rulesets"] = rulesets or []
        cache["base"] = pr["baseRefName"] if rulesets is not None else None

    return build_snapshot(data, cache.get("rulesets"), extra, skip_ok=target["skipOk"])


# ---------------------------------------------------------------- command line

_DURATION_RE = re.compile(r"^(\d+)([smh]?)$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_duration(value, name):
    m = _DURATION_RE.match(str(value))
    if not m:
        raise UsageError(
            "%s: expected a number of seconds or e.g. 90s, 30m, 2h (got %s)" % (name, value)
        )
    return int(m.group(1)) * _UNITS[m.group(2)]


USAGE = (
    "usage: pr_merge_watch.py <status|wait> <PR number|URL> [--repo owner/name] [--no-auto] "
    "[--no-review] [--skip-ok[=check,...]] [--interval 60] [--timeout 0] [--grace 600]"
)


def parse_args(argv, repo_lookup=None):
    mode = argv[0] if argv else None
    rest = list(argv[1:])
    o = {
        "mode": mode, "repo": None, "pr": None, "noAuto": False, "noReview": False,
        "skipOk": False, "interval": 60, "timeout": 0, "grace": 600,
    }
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--repo":
            i += 1
            o["repo"] = rest[i] if i < len(rest) else None
            if not re.match(r"^[^/\s]+/[^/\s]+$", o["repo"] or ""):
                raise UsageError("usage: --repo expects owner/name (got %s)" % o["repo"])
        elif a == "--no-auto":
            o["noAuto"] = True
        elif a == "--no-review":
            o["noAuto"] = o["noReview"] = True
        elif a == "--skip-ok":
            o["skipOk"] = True
        elif a.startswith("--skip-ok="):
            o["skipOk"] = [n.strip() for n in a[10:].split(",") if n.strip()]
        elif a == "--interval":
            i += 1
            o["interval"] = max(MIN_INTERVAL, parse_duration(rest[i] if i < len(rest) else None, a))
        elif a == "--timeout":
            i += 1
            o["timeout"] = parse_duration(rest[i] if i < len(rest) else None, a)
        elif a == "--grace":
            i += 1
            o["grace"] = parse_duration(rest[i] if i < len(rest) else None, a)
        elif not a.startswith("--") and o["pr"] is None:
            o["pr"] = a
        else:
            raise UsageError("unknown argument: %s" % a)
        i += 1

    if mode not in ("status", "wait") or o["pr"] is None:
        raise UsageError(USAGE)

    url = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", o["pr"])
    if url:
        o["repo"] = o["repo"] or url.group(1)
        o["pr"] = url.group(2)
    if not re.match(r"^\d+$", o["pr"]):
        raise UsageError("not a pull request number or URL: %s" % o["pr"])
    if not o["repo"]:
        lookup = repo_lookup or (
            lambda: gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
        )
        o["repo"] = lookup().strip()
    o["number"] = int(o["pr"])
    return o


def log(message):
    sys.stderr.write("[%sZ] %s\n" % (now_iso()[11:19], message))


def emit(s, events, opts):
    s["event"] = events[0]
    if len(events) > 1:
        s["also"] = events[1:]
    s["hint"] = hint(s, **opts)
    sys.stdout.write(json.dumps(s, indent=2, ensure_ascii=False) + "\n")


def mode_of(o, s):
    """How the merge is meant to happen: the command-line flags, or else the mode recorded on the PR."""
    no_review = o["noReview"] or s["markers"]["mode"] == "unreviewed"
    return {
        "no_auto": o["noAuto"] or no_review or s["markers"]["mode"] == "manual",
        "no_review": no_review,
    }


def run(argv):
    o = parse_args(argv)
    cache = {}

    if o["mode"] == "status":
        s = fetch_snapshot(o, cache)
        # GitHub computes mergeability lazily; give it a moment right after a push or `gh pr ready`.
        for _ in range(5):
            if not (s["pr"]["state"] == "OPEN" and s["pr"]["mergeStateStatus"] == "UNKNOWN"):
                break
            time.sleep(3)
            s = fetch_snapshot(o, cache)
        opts = mode_of(o, s)
        events = classify(s, **opts)
        emit(s, events or ["waiting"], opts)
        return 0

    started = time.time()
    since = {}
    failures = 0
    last_line = ""
    while True:
        try:
            s = fetch_snapshot(o, cache)
            failures = 0
        except Exception as e:
            # A network blip must not end a watch that may run for hours; a broken login must.
            # Everything is retried, not just GhError: a half-written GraphQL answer surfaces
            # as a TypeError or KeyError here, and the original caught those the same way.
            failures += 1
            if getattr(e, "fatal", False) or failures >= 10:
                raise
            log("gh 호출 실패 (%d/10): %s" % (failures, _describe(e).split("\n")[0]))
            time.sleep(o["interval"])
            continue

        opts = mode_of(o, s)
        events = classify(s, expect_auto=not opts["no_auto"], **opts)
        events = events + timed_events(
            s, opts, since, time.time() * 1000, o["grace"] * 1000
        )
        if events:
            emit(s, events, opts)
            return 0
        remaining = (o["timeout"] - (time.time() - started)) if o["timeout"] else None
        if remaining is not None and remaining <= 0:
            emit(s, ["timeout"], opts)
            return 0
        s["event"] = "waiting"
        line = hint(s, **opts)
        if line != last_line:
            last_line = line
            log(line)
        time.sleep(o["interval"] if remaining is None else min(o["interval"], remaining))


def _describe(e):
    """An error line that says where to look.

    A bare str() on an unexpected exception reads as gh's fault: a KeyError prints as
    just 'viewer'. Errors that are about gh keep their own message.
    """
    if isinstance(e, (GhError, OSError)):
        return str(e)
    return "%s: %s" % (type(e).__name__, e)


def _error(hint):
    sys.stdout.write(
        json.dumps({"event": "error", "hint": hint}, indent=2, ensure_ascii=False) + "\n"
    )


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        # Inside the try: a handover that fails to start must still leave the skill an
        # `event` to read. (On POSIX a started handover has replaced this process, so
        # what happens after that - Ctrl+C included - is the newer interpreter's.)
        outcome = ensure_floor(argv)
        if outcome is not None:
            kind, value = outcome
            if kind == "exit":
                return value
            _error(value)
            return 1
        return run(argv)
    except UsageError as e:
        _error(str(e))
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        # The caller's contract is a JSON object on stdout, always. Letting anything
        # escape as a traceback leaves the skill with no `event` to read, so every
        # remaining exception is reported the same way - with its type, since an
        # unexpected one needs more than its message to place.
        _error(_describe(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())

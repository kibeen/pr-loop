"""hooks/research_nudge.py, run the way Claude Code runs it: JSON on stdin, JSON on stdout.

  python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "research_nudge.py"
HOOKS_JSON = ROOT / "hooks" / "hooks.json"


def run(stdin: str, encoding: str | None = None) -> subprocess.CompletedProcess:
    """Run the hook. `encoding` forces the interpreter's stdio encoding, the way a
    Windows console code page does."""
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)
    if encoding:
        env["PYTHONIOENCODING"] = encoding
    return subprocess.run([sys.executable, str(HOOK)], input=stdin.encode("utf-8"),
                          capture_output=True, timeout=30, env=env)


def skill_event(name) -> str:
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Skill",
                       "tool_input": {"skill": name},
                       "tool_response": {"success": True, "commandName": name}})


class ResearchNudgeTest(unittest.TestCase):
    def context_for(self, skill: str, encoding: str | None = None) -> str:
        r = run(skill_event(skill), encoding)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        return out["additionalContext"]

    def test_brainstorming_is_told_to_research_before_the_approaches(self):
        text = self.context_for("superpowers:brainstorming")
        self.assertIn("`prior-art-research`", text)
        self.assertIn("접근법을 제시하기 전에", text)

    def test_debugging_is_told_to_search_public_issues(self):
        text = self.context_for("superpowers:systematic-debugging")
        self.assertIn("공개 이슈", text)

    def test_searches_are_told_to_keep_private_names_out(self):
        """The debugging nudge says it itself; the design nudge hands over to the
        skill, whose first step says it."""
        self.assertIn("비공개", self.context_for("superpowers:systematic-debugging"))
        skill = (ROOT / "skills" / "prior-art-research" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("검색어에는 공개된 기술 개념만 쓴다", skill)

    def test_the_bare_skill_name_gets_the_same_nudge(self):
        for name in ("brainstorming", "systematic-debugging"):
            self.assertEqual(self.context_for(name), self.context_for("superpowers:" + name))

    def test_a_same_named_skill_of_another_plugin_gets_nothing(self):
        r = run(skill_event("other-plugin:brainstorming"))
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_a_legacy_code_page_cannot_break_the_output(self):
        """Windows consoles often run cp1252, which cannot encode the Korean text.
        The hook crashed there, and the `|| python3` fallback then read an empty
        stdin and printed nothing, so the instruction was lost without an error."""
        for encoding in ("cp1252", "ascii"):
            text = self.context_for("superpowers:brainstorming", encoding)
            self.assertIn("접근법을 제시하기 전에", text, encoding)

    def test_non_ascii_input_is_read(self):
        event = json.loads(skill_event("superpowers:systematic-debugging"))
        event["tool_response"] = {"note": "한글 출력과 ✓"}
        for encoding in (None, "cp1252"):
            r = run(json.dumps(event, ensure_ascii=False), encoding)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(b"additionalContext", r.stdout)

    def test_other_skills_get_nothing(self):
        for stdin in (skill_event("superpowers:writing-plans"), skill_event(None), skill_event(""),
                      json.dumps({"tool_name": "Skill"}), "null", "[]", '"text"',
                      json.dumps({"tool_input": "superpowers:brainstorming"}),
                      json.dumps({"tool_input": {"skill": ["superpowers:brainstorming"]}})):
            r = run(stdin)
            self.assertEqual((r.returncode, r.stdout), (0, b""), stdin)

    def test_broken_input_never_fails_the_tool_call(self):
        r = run("not json")
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_the_skill_the_nudge_names_exists(self):
        named = set(re.findall(r"스킬 `([\w-]+)`", HOOK.read_text(encoding="utf-8")))
        self.assertTrue(named)
        for name in named:
            self.assertTrue((ROOT / "skills" / name / "SKILL.md").is_file(), name)

    def test_the_nudge_names_no_plugin_namespace(self):
        """The namespace follows the installed plugin's name, which can differ from
        plugin.json while a marketplace entry is being renamed."""
        for nudge in self.hook_module().NUDGES.values():
            self.assertIsNone(re.search(r"`[\w-]+:[\w-]+`", nudge), nudge)

    def hook_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("research_nudge", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_hooks_json_runs_this_script_after_the_skill_tool(self):
        hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
        entries = hooks["PostToolUse"]
        self.assertEqual([e["matcher"] for e in entries], ["Skill"])
        for h in entries[0]["hooks"]:
            self.assertEqual(h["type"], "command")
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/research_nudge.py", h["command"])


if __name__ == "__main__":
    unittest.main()

"""hooks/research_nudge.py, run the way Claude Code runs it: JSON on stdin, JSON on stdout.

  python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "research_nudge.py"
HOOKS_JSON = ROOT / "hooks" / "hooks.json"


def run(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HOOK)], input=stdin.encode("utf-8"),
                          capture_output=True, timeout=30)


def skill_event(name) -> str:
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Skill",
                       "tool_input": {"skill": name},
                       "tool_response": {"success": True, "commandName": name}})


class ResearchNudgeTest(unittest.TestCase):
    def context_for(self, skill: str) -> str:
        r = run(skill_event(skill))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        return out["additionalContext"]

    def test_brainstorming_is_told_to_research_before_the_approaches(self):
        text = self.context_for("superpowers:brainstorming")
        self.assertIn("seekflow:prior-art-research", text)
        self.assertIn("접근법을 제시하기 전에", text)

    def test_debugging_is_told_to_search_public_issues(self):
        text = self.context_for("superpowers:systematic-debugging")
        self.assertIn("공개 이슈", text)

    def test_every_nudge_keeps_private_names_out_of_queries(self):
        for skill in ("superpowers:brainstorming", "superpowers:systematic-debugging"):
            text = self.context_for(skill)
            self.assertTrue("비공개" in text or "prior-art-research" in text, skill)

    def test_other_skills_get_nothing(self):
        for stdin in (skill_event("superpowers:writing-plans"), skill_event(None),
                      json.dumps({"tool_name": "Skill"}), "null", "[]"):
            r = run(stdin)
            self.assertEqual((r.returncode, r.stdout), (0, b""), stdin)

    def test_broken_input_never_fails_the_tool_call(self):
        r = run("not json")
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_the_skill_the_nudge_names_exists(self):
        named = set(re.findall(r"seekflow:([\w-]+)", HOOK.read_text(encoding="utf-8")))
        self.assertTrue(named)
        for name in named:
            self.assertTrue((ROOT / "skills" / name / "SKILL.md").is_file(), name)

    def test_the_plugin_is_named_as_the_nudge_says(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "seekflow")

    def test_hooks_json_runs_this_script_after_the_skill_tool(self):
        hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
        entries = hooks["PostToolUse"]
        self.assertEqual([e["matcher"] for e in entries], ["Skill"])
        for h in entries[0]["hooks"]:
            self.assertEqual(h["type"], "command")
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/research_nudge.py", h["command"])


if __name__ == "__main__":
    unittest.main()

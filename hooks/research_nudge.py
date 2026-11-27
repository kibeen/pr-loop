"""PostToolUse hook on the Skill tool: when a design or debugging skill loads, tell
the model when to look outside before choosing an approach.

superpowers leaves web research optional, so it rarely happens. This adds one
instruction next to the skill's text, at the moment the skill loads, and nothing
else: it reads stdin, writes JSON to stdout, and never fails the tool call.

Input and output go through the byte streams. The console encoding on Windows is
often a legacy code page, which cannot write the Korean text, and a crash there
would leave the `|| python3` fallback in hooks.json an empty stdin and print
nothing. The output is ASCII JSON (non-ASCII as \\u escapes), so no encoding
setting can break it.

The skill is named without its plugin namespace: the namespace follows the
installed plugin's name, which this file cannot know, and the model resolves a
bare skill name against the skills it is shown.
"""
from __future__ import annotations

import json
import sys

NUDGES = {
    "brainstorming": (
        "이 설계가 새 소프트웨어 기능, 알고리즘, 구조에 관한 것이면 접근법을 제시하기 전에 "
        "스킬 `prior-art-research` 로 선행 조사(대표 시스템의 설계, 관련 논문, 비교 평가)를 하고 "
        "그 결과로 후보를 만든다. 이 대화에서 이미 조사했으면 그 결과를 쓴다. "
        "소프트웨어 설계가 아니거나 버그 수정, 문서, 설정 변경이면 건너뛴다."
    ),
    "systematic-debugging": (
        "원인이 외부 라이브러리, 프레임워크, 도구의 동작일 수 있으면 가설을 세우기 전에 "
        "오류 문구와 버전으로 공개 이슈, 릴리스 노트, 문서를 검색한다. "
        "검색어에는 비공개 코드, 경로, 내부 이름을 넣지 않는다."
    ),
}


def nudge_key(skill: str | None) -> str | None:
    """The superpowers skill a loaded name refers to: `superpowers:brainstorming`,
    or the bare `brainstorming` the model may also call it by. A same-named skill of
    another plugin is not superpowers' and gets nothing."""
    if not skill:
        return None
    plugin, _, name = skill.rpartition(":")
    return name if plugin in ("", "superpowers") else None


def skill_of(event: object) -> str | None:
    """The skill a Skill tool call loaded, or None for any other shape."""
    if not isinstance(event, dict):
        return None
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    skill = tool_input.get("skill")
    return skill if isinstance(skill, str) else None


def main() -> int:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except ValueError:
        return 0
    nudge = NUDGES.get(nudge_key(skill_of(event)))
    if nudge:
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": nudge}}
        sys.stdout.buffer.write(json.dumps(out, ensure_ascii=True).encode("ascii"))
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

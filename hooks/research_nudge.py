"""PostToolUse hook on the Skill tool: when a design or debugging skill loads, tell
the model when to look outside before choosing an approach.

superpowers leaves web research optional, so it rarely happens. This adds one
instruction next to the skill's text, at the moment the skill loads, and nothing
else: it reads stdin, writes JSON to stdout, and never fails the tool call.
"""
from __future__ import annotations

import json
import sys

NUDGES = {
    "superpowers:brainstorming": (
        "이 설계가 새 소프트웨어 기능, 알고리즘, 구조에 관한 것이면 접근법을 제시하기 전에 "
        "스킬 `seekflow:prior-art-research` 로 선행 조사(대표 시스템의 설계, 관련 논문, 비교 평가)를 하고 "
        "그 결과로 후보를 만든다. 이 대화에서 이미 조사했으면 그 결과를 쓴다. "
        "소프트웨어 설계가 아니거나 버그 수정, 문서, 설정 변경이면 건너뛴다."
    ),
    "superpowers:systematic-debugging": (
        "원인이 외부 라이브러리, 프레임워크, 도구의 동작일 수 있으면 가설을 세우기 전에 "
        "오류 문구와 버전으로 공개 이슈, 릴리스 노트, 문서를 검색한다. "
        "검색어에는 비공개 코드, 경로, 내부 이름을 넣지 않는다."
    ),
}


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    skill = ((event or {}).get("tool_input") or {}).get("skill")
    nudge = NUDGES.get(skill)
    if nudge:
        json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                          "additionalContext": nudge}},
                  sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

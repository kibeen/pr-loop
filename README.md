# pr-loop

구현 PR 을 draft 리뷰부터 머지까지 끌고 가는 Claude Code 플러그인이다. 코드를 바꾼 PR 은 크기와 관계없이
독립 리뷰, CI, 필요한 평가를 거치고, 그 조건이 모두 차면 사용자에게 묻지 않고 머지될 때까지 맡는다. 조건에 못 미치면 멈춰 보고한다.

## 설치
[kibeen-skills](https://github.com/kibeen/kibeen-skills) 마켓플레이스가 이 레포를 가리킨다.
```
claude plugin marketplace add kibeen/kibeen-skills
claude plugin install pr-loop@kibeen-skills
```
`gh` CLI 와 Python 3.11+ 가 필요하다(표준 라이브러리만 쓴다). Node.js 는 필요 없다.
하한의 단일 출처는 `pr_merge_watch.py` 의 `MIN_PYTHON` 이고, 이 README 와 CI 워크플로가 그것과 다른 버전을 말하면 테스트가 실패한다.
돌고 있는 파이썬이 하한보다 낮으면 스크립트가 더 새 인터프리터를 스스로 찾아(`python3.14`…`python3.11`, `python3`, `python`, `py -3`)
같은 명령을 넘긴다. 머신에 하나도 없으면 설치 명령을 한 줄로 알려 준다(Windows `winget`, macOS `brew`,
Linux 는 패키지 관리자 감지) — 설치는 직접 하지 않는다. 런타임 설치는 시스템 변경이기 때문이다. `plugin.json` 에 `version` 을 적지 않으므로 커밋 SHA 가 버전이고,
이 레포에 푸시하면 `claude plugin update pr-loop@kibeen-skills` 로 받는다.

## 구성
| 구성 | 종류 | 하는 일 |
|---|---|---|
| `pr-review-loop` | 스킬 | draft PR 을 만들고 리뷰 서브에이전트의 리뷰와 반영을 self-approve 까지 반복한다(최대 7회). 그 뒤 CI 통과까지의 수정과 PoC 평가를 함께 진행한다. 지금 head 의 self-approve, CI 통과, 평가의 합격 기준 충족이 모두 차면 묻지 않고 `pr-merge-loop` 로 넘어가고, 기준 미달이면 멈춰 보고한다. CI 수정이 평가한 동작을 바꾸면 평가를 중단하고 재리뷰 뒤 다시 잰다 |
| `pr-merge-loop` | 스킬 | 그 조건을 채운 PR(또는 사용자가 머지를 지시한 PR)을 열고(ready) auto-merge 를 켠 뒤, 필수 CI 실패와 리뷰어 코멘트를 처리해 머지될 때까지 감시한다. 리뷰가 필수가 아닌 base 는 요청된 리뷰어의 승인을 기다리고, 본인 소유의 개인 저장소면 CI 통과 뒤 머지한다. 리뷰 요청을 반영하지 않겠다는 답글만 문안을 사용자에게 보여 승인받은 뒤 올린다 |
| `pr-reviewer` | 서브에이전트 | `pr-review-loop` 의 독립 리뷰어. 코드 결함과 평가 계획을 보고 PR 코멘트로 VERDICT 를 남긴다. 코드를 고치지 않는다 |
| `pr_merge_watch.py` | 스킬 스크립트 | `pr-merge-loop` 의 감시 도구(읽기 전용). `status` 는 PR 상태를 한 번 읽고, `wait` 는 할 일이 생길 때까지 기다린다(필수 체크 실패, 새 리뷰 코멘트, base 와 충돌, 머지 완료) |

두 스킬은 진행 상태를 PR 코멘트의 표시(`<!-- pr-review-loop step=... -->`)로 남긴다. 세션이 바뀌어도 그 표시를 읽어 이어 간다.

## 레이아웃
```
.claude-plugin/plugin.json
skills/pr-review-loop/SKILL.md
skills/pr-merge-loop/SKILL.md
skills/pr-merge-loop/scripts/pr_merge_watch.py
agents/pr-reviewer.md
tests/test_pr_merge_watch.py
```

## 테스트
```
python -m unittest discover -s tests
```
네트워크와 `gh` 없이 돈다. 스킬 본문을 고칠 때는 서브에이전트에게 시나리오를 풀게 해 절차대로 따라가는지 확인하고, 독립 리뷰를 거친다.

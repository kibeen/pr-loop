---
name: pr-merge-loop
description: pr-review-loop 의 머지 진행 조건(리뷰 self-approve, CI 통과, 평가의 합격 기준 충족)을 채운 PR 이나 사용자가 머지를 지시한 PR 을 열어 머지될 때까지 맡을 때 쓰는 절차. 조건이 차면 사용자에게 묻지 않고 시작한다. pr-review-loop 이 넘겨줄 때, 또는 "PR 열어줘", "auto-merge 켜줘", "머지까지 봐줘", "CI 통과까지 고쳐줘", "리뷰 코멘트 확인해줘", "PR 이어서 봐줘" 처럼 이미 올라간 PR 의 필수 CI 실패와 리뷰어 코멘트를 처리해 달라는 요청에 쓴다.
---

# PR 머지 루프

목적: 절차를 채운 PR 을 ready 로 열고 auto-merge 를 켠 뒤, 머지를 막는 CI 실패와 리뷰어 코멘트를 처리한다.
PR 이 `MERGED` 가 되면 끝난다.

시작 조건은 둘 중 하나다.
- `pr-review-loop` 7단계의 4 가 찼다: 지금 head 의 self-approve, CI 통과(돌아서 통과한 체크 1건 이상), 평가 계획이 있으면
  `기준 충족` 판정과 리뷰어의 `평가 확인: 동의`, PR 본문에 `머지 전 사용자 확인` 줄 없음. 이때는 사용자에게 묻지 않고 시작한다.
- 사용자가 이 PR 의 머지를 지시했다(멈춰 보고한 뒤의 `머지`, 이미 올라간 PR 을 머지까지 봐 달라는 요청).

조건이 차지 않았는데 결과가 좋아 보인다는 이유로 시작하지 않는다. 리뷰어의 Approve 와 사용자의 무응답도 시작 조건이 아니다.

이 절차가 하는 일은 단계마다 묻지 않고 진행한다: PR 열기, auto-merge, 수정 커밋 푸시, 진행 코멘트, 리뷰어 답글과 재리뷰 요청,
base 규칙이 허용하는 머지. 예외는 둘이다.
- 리뷰 지적을 **반영하지 않겠다는 답**(`안 고침`)은 문안을 사용자에게 보여 승인을 받은 뒤에 올린다(3.2, 3.4).
- 프로젝트 규칙이 따로 허락을 요구하는 동작(예: 릴리스 브랜치 머지)은 그 허락을 받는다. 1단계를 시작하기 전에 묻는다.

리뷰어 답글에는 확인한 사실과 한 일만 쓴다. 사용자가 정할 것(범위, 설계, 일정)을 대신 약속하지 않는다.

## 상태 읽기와 기록
상태는 감시 스크립트로 읽는다(이 스킬 폴더의 `scripts/pr_merge_watch.py`, PR 을 바꾸지 않는다).
아래에서 `필드.이름` 은 이 출력의 JSON 필드다. `hint` 는 지금 상태를 한 줄로 요약한다.
```bash
python "${CLAUDE_SKILL_DIR}/scripts/pr_merge_watch.py" status <PR> [--repo owner/name]   # 지금 상태 한 번
python "${CLAUDE_SKILL_DIR}/scripts/pr_merge_watch.py" wait <PR> [--repo owner/name]     # 내가 할 일이 생길 때까지 기다림
```
`python` 이 없는 환경에서는 `python3` 로 바꿔 실행한다. 표준 라이브러리만 쓰므로 설치할 것은 없다.
파이썬이 하한보다 낮으면 스크립트가 더 새 인터프리터를 찾아 같은 명령을 넘기고, 하나도 없으면
`{"event": "error", "hint": "... 설치 명령"}` 을 돌려준다. 그 명령을 사용자에게 전하고 지시를 기다린다.

진행 상태는 PR 코멘트 첫 줄의 표시로 남긴다(pr-review-loop 과 같은 형식). `round` 는 그 step 의 순번이고 `markers.next.<step>` 값을 쓴다.
`sha` 는 코멘트를 올리는 시점의 head 다.
`<!-- pr-review-loop step=<open|hold|ci|feedback> round=<N> sha=<head 커밋> -->`

| step | 뜻 | 언제 올리나 |
|---|---|---|
| `open` | 머지 진행을 시작했다. 마지막 `open` 의 sha 가 절차를 채운 커밋(기준 커밋, 필드 이름은 `markers.acceptedSha`)이다. `mode=auto\|manual\|unreviewed` 를 덧붙인다 | 진행을 시작할 때(1단계), 머지 방식을 바꿀 때, 보류가 풀려 다시 진행할 때 |
| `hold` | 사용자 결정을 기다린다. 머지하지 않는다 | 사용자에게 물을 때(2단계의 선택, 3.4). 다음 `open` 이 보류를 닫는다 |
| `ci` | CI 실패를 처리했다 | 3.1 |
| `feedback` | 리뷰어 코멘트를 처리했다. `until=<시각>` 을 덧붙인다. `안 고침` 문안이 사용자 승인을 기다리는 항목은 `ask=<id,...>` 로 적는다 | 3.2 |

새 세션에서 이어받을 때는 `status` 를 한 번 돌린다.
- `markers.hold` 가 있으면 그 코멘트의 질문이 아직 열려 있다. 답이 대화에 없으면 사용자에게 다시 묻는다.
  `markers.reevaluating` 이 true 면(보류 뒤에 `review`, `fix`, `poc` 표시가 있다) 재평가 중이므로 pr-review-loop 으로 이어간다.
- `markers.acceptedSha` 가 있으면 머지 진행 중인 PR 이다. `markers.mode` 가 `auto` 가 아니거나 `autoMerge.enabled` 가 true 면 3단계부터 한다.
  `mode` 가 `auto` 인데 auto-merge 가 꺼져 있으면 `autoMerge.disabledBy` 를 본다. 없으면 켠 적이 없는 것이니 2단계부터 한다.
  있으면 누군가 끈 것이다. 다시 켜지 않고 사용자에게 보고한다.
- 둘 다 없으면 아직 머지 진행 전이다. pr-review-loop 으로 돌아가 7단계의 4 가 찼는지 본다.
- `feedback.awaitingUser` 에 항목이 있으면 `안 고침` 답글 문안이 사용자 승인을 기다리는 중이다. 문안은 PR 에 남아 있지 않으므로
  다시 써서 보여 준다(3.2 의 5번). 마지막 반영 코멘트에 `답하지 않음(사용자 결정)` 으로 적힌 항목은 다시 묻지 않는다.

## 1. 열기
1. `status` 를 돌리고 머지 방식을 정한다. 레포 규칙(레포 `CLAUDE.md`, 기여 문서)에 적힌 방식을 쓰고, 없으면 `repo.defaultMergeMethod` 를 쓴다.
2. open 코멘트를 올려 진행을 기록한다. 사용자 지시로 시작했으면 근거를 `사용자 지시` 로 쓴다.
   ```
   <!-- pr-review-loop step=open round=<markers.next.open> sha=<head> mode=auto -->
   진행: 절차 충족(리뷰 self-approve 라운드 <N>, CI 통과, 평가 <기준 충족 | 없음>). 기준 커밋 <sha>, 머지 방식 <방식>.
   ```
3. PR 본문을 리뷰어가 읽을 최종본으로 고친다(`gh pr edit <PR> --body-file <본문.md>`): 변경 요약, 테스트,
   평가 결과 요약과 결과 코멘트 링크, 평가한 커밋. 프로젝트의 글쓰기 규칙이 있으면 따른다.
4. draft 면 `gh pr ready <PR>`.
5. `status` 를 다시 돌려 `autoMerge.effect` 를 본다.

## 2. auto-merge 켜기

auto-merge 는 base 브랜치가 **필수로 정한** 리뷰와 체크만 기다린다. 필수가 아닌 것은 기다리지 않고 머지하므로,
켜기 전에 무엇을 기다릴지 `autoMerge.effect` 로 확인한다(사유는 `autoMerge.note`). auto-merge 가 이미 켜져 있어도
판정은 같다. `no_review`, `not_enforced`, `ci_not_run`, `ci_not_green` 이면 먼저 끄고(`gh pr merge <PR> --disable-auto`) 그 행을 따른다.

| `autoMerge.effect` | 뜻 | 할 일 |
|---|---|---|
| `arms` | 리뷰와 필수 체크를 기다렸다가 머지된다 | `gh pr merge <PR> --auto --<방식>` |
| `merges_now` | 사람이 승인했고 체크도 다 찼다. 켜는 순간 머지된다 | 같은 명령. 규칙이 허용하고 절차를 채운 머지다 |
| `no_review` | base 가 리뷰를 요구하지 않고 이 커밋을 승인한 사람도 없다. `--auto` 는 **리뷰 없이** 머지한다(지금 바로, 또는 필수 체크가 통과하는 즉시) | 실행하지 않는다. 아래 "리뷰가 필수가 아닌 base" 대로 `mode` 를 정한다 |
| `not_enforced` | base 가 리뷰나 CI 중 하나를 필수로 걸지 않았다. auto-merge 는 그것을 기다리지 않는다(승인만으로 CI 없이, 또는 푸시로 승인이 낡아도 그대로 머지) | 켜지 않는다. `mode=manual` 인 open 코멘트를 올리고 3단계로 간다. 사용자에게 승인과 CI 통과 뒤 직접 머지한다고 알린다 |
| `ci_not_run` | 필수 체크가 한 번도 실행되지 않고 skip 됐다(`checks.skipped`). GitHub 는 이것을 통과로 친다 | 레포의 CI 실행 규칙(라벨, 수동 실행)대로 CI 를 먼저 돌리고 `status` 를 다시 본다. 그 skip 이 레포에서 정상(경로 필터 등)이면 `--skip-ok=<체크 이름,...>` 을 붙인다 |
| `ci_not_green` | GitHub 는 머지 가능으로 보지만 실행된 체크 중 통과하지 못한 것이 있다(예: 실패한 필수 체크를 뒤의 skip 된 실행이 덮음). `--auto` 는 그대로 머지한다 | 켜지 않는다. 3.1 로 CI 를 먼저 통과시키고 `status` 를 다시 본다 |
| `not_allowed` | 저장소가 auto-merge 를 꺼 두었다 | 켤 수 없으므로 내가 머지한다. base 가 리뷰를 요구하면(`rules.requiredApprovals` 가 1 이상이거나 `review.decision` 이 null 이 아니다) `mode=manual`, 아니면 아래 "리뷰가 필수가 아닌 base" 대로 `mode` 를 정한다 |
| `enabled` | 이미 켜져 있고 리뷰와 필수 체크를 기다린다 | `autoMerge.method` 가 맞는지만 확인 |
| `unknown` | GitHub 가 아직 계산 중이다 | `status` 를 다시 돌린다 |

`draft` 가 나오면 1단계의 `gh pr ready` 가 안 된 것이고, `closed` 면 PR 이 닫힌 것이다.

리뷰가 필수가 아닌 base(`no_review`, 리뷰를 요구하지 않는 `not_allowed`)에서는 아래 순서로 `mode` 를 정해 open 코멘트를 올리고 3단계로 간다.
1. 요청된 사람 리뷰어가 있다(`review.requestedPeople`. 봇은 들어 있지 않다. `manual` 은 사람의 승인만 치므로 봇만 요청된 PR 은 기다릴 사람이 없다):
   `mode=manual`. 그 사람의 승인을 기다렸다가 내가 머지한다.
2. 요청된 사람 리뷰어가 없지만 레포 규칙이 리뷰어를 정해 두었다: 그 리뷰어를 요청하고(`gh pr edit <PR> --add-reviewer <login>`) `mode=manual`.
3. 요청된 사람 리뷰어가 없고 `repo.personal` 이 true 다(`gh` 계정 본인 소유의 개인 저장소, 리뷰할 사람이 없다): `mode=unreviewed`.
   묻지 않는다. 모든 체크가 통과하면 `ready` 가 오고 그때 내가 머지한다.
4. 그 밖(남의 저장소나 조직 저장소인데 리뷰할 사람이 정해지지 않았다): hold 코멘트를 올리고 묻는다(지금 CI 상태도 함께).
   **리뷰 없이 머지**(`mode=unreviewed`) / **리뷰 승인 뒤 머지**(`mode=manual`, 리뷰어를 받아 요청한다) / **그대로 둠**(open 코멘트 없이 보류를 둔 채 보고하고 끝낸다).

어느 `mode` 든 실패한 CI 를 둔 채 머지하지 않는다. `mode=unreviewed` 는 체크가 유일한 관문이므로 이 head 에서 실제로 돌아 통과한 체크가
하나도 없으면(`checks.succeeded` 가 0: 붙지 않았거나 skip 만 됐다) 머지하지 않는다. 그때는 `ready` 대신 `ci_not_started` 가 온다.

**CI 가 없는 저장소**: 돌릴 CI 가 없으면 스스로 머지하지 않고 멈춰 보고한다. 사용자가 그 보고를 받고 `CI 없이 머지` 를 지시했을 때만
`mode=unreviewed` open 코멘트의 본문에 `사용자 지시: CI 없이 머지` 를 적고 직접 머지한다(`gh pr merge <PR> --<방식>`).
이때는 `ready` 가 오지 않으므로 기다리지 않는다. 머지 전에 `status` 로 실패했거나 진행 중인 체크가 없고, 변경 요청과 충돌이 없는지 본다.
이 지시는 그 PR 한 건에만 해당한다.

**사용자가 머지를 멈추라고 할 때**(진행 중 언제든): auto-merge 가 켜져 있으면 먼저 끈다(`gh pr merge <PR> --disable-auto`).
hold 코멘트(`보류: 사용자 지시로 머지 중지.`)를 올리고 PR 본문의 진행 상태 절에 `머지 전 사용자 확인: <사유>` 줄을 넣는다.
감시는 `hold` 에서 멈추고, 다음 세션도 `markers.hold` 로 이 보류를 읽는다. 사용자가 다시 머지를 지시하면 본문의 줄을 지우고
open 코멘트(`재개: 사용자 지시`)로 보류를 닫은 뒤 2단계부터 다시 한다.

hold 코멘트는 질문을 한 줄로 적는다.
```
<!-- pr-review-loop step=hold round=<markers.next.hold> sha=<head> -->
보류: <무엇을 정해야 하는지>. 사용자 결정 대기.
```
사용자의 답을 받으면 open 코멘트를 올려 보류를 닫고 3단계로 간다.

`mode=manual` 과 `mode=unreviewed` 에서는 auto-merge 를 켜지 않고 내가 유일한 관문이 된다. `manual` 의 `ready` 는 사람이
**지금 head 를** 승인했고(`review.approvals`), 변경 요청이 없고, 모든 체크가 통과했을 때만 온다. 이전 커밋의 승인
(`review.staleApprovals`)과 봇의 승인은 치지 않는다. `unreviewed` 의 `ready` 는 승인 없이 체크 통과만 본다.

`mode=auto` 나 `mode=manual` 인데 리뷰할 사람이 없으면(`review.requested` 가 비었고 `review.approvals` 도 비었다) 사용자에게 알린다.
리뷰어를 정해 주면 `gh pr edit <PR> --add-reviewer <login>`. 감시는 그동안에도 돌린다.

## 3. 감시 루프
`wait` 를 **백그라운드로** 띄운다(Bash 의 `run_in_background`). 내가 할 일이 생기면 스크립트가 끝나고 하네스가 다시 불러 준다.
그동안 폴링하거나 sleep 하지 않는다. 띄울 때 무엇을 기다리는지 사용자에게 한 줄로 알린다(`hint`).
기본은 시간 제한이 없다. 백그라운드 실행이 없는 환경이면 `--timeout 9m` 을 붙여 반복한다.
세션이 끝나면 감시도 끝난다. 다음 세션이 `status` 로 이어받는다.

끝나면 출력의 `event`(깨운 이유)와 `also`(함께 열려 있는 이벤트)를 모두 처리하고 다시 `wait` 를 띄운다.
수정 커밋을 푸시할 때마다 레포의 CI 실행 규칙을 다시 적용한다(라벨로 CI 를 돌리는 레포는 라벨을 다시 붙인다).

| `event` | 할 일 |
|---|---|
| `merged` | 4단계 |
| `ci_failed` | 3.1 |
| `feedback` | 3.2 |
| `behind`, `conflict` | 3.3 |
| `hold` | 사용자 결정을 기다리는 보류가 열려 있다. `autoMerge.enabled` 가 true 면 먼저 끈다. 답을 받기 전에는 `wait` 를 다시 띄우지 않는다 |
| `ci_not_started` | `checks.missingRequired`(보고되지 않음)나 `checks.skipped`(skip 만 됨)의 필수 체크가 돌지 않았다. `mode=unreviewed` 에서는 돌아서 통과한 체크가 하나도 없을 때도 온다(이때는 `--skip-ok` 로 넘기지 않는다. 돌릴 CI 가 없으면 2단계의 "CI 가 없는 저장소" 대로 멈춰 보고한다). 레포의 CI 실행 규칙대로 돌린다. 그 skip 이 레포에서 정상이면 `--skip-ok=<체크 이름,...>` 을 붙여 다시 `wait`. 규칙을 모르면 보고한다 |
| `ready` | (`mode=manual`, `unreviewed`) 머지 조건이 찼다. `gh pr merge <PR> --<방식>`. 머지가 실패하면 다시 `wait` 하지 않고 오류를 보고한다 |
| `auto_merge_off` | `autoMerge.disabledBy` 가 있으면 누군가 끈 것이다. 다시 켜지 않고 보고한다. 없으면 켠 적이 없는 것이니 2단계를 한다 |
| `auto_merge_unsafe` | auto-merge 가 켜져 있는데 리뷰나 CI 를 기다려 주지 않는 상태가 됐다(예: 푸시로 승인이 낡음). 먼저 끄고 2단계의 표를 다시 따른다 |
| `stuck` | 승인과 체크가 찼는데 머지되지 않는다. `pr.mergeStateStatus`, `review.unresolvedThreads`, `rules` 로 원인을 찾고, 못 찾으면 보고한다 |
| `closed` | 머지 없이 닫혔다. 보고하고 끝낸다 |
| `draft` | `markers.acceptedSha` 가 없으면 1단계가 끝나지 않은 것이다. 있으면 누군가 draft 로 되돌린 것이다. `gh pr ready` 를 다시 하지 않고 보고한다 |
| `timeout` | 다시 `wait` |
| `error` | 스크립트가 PR 을 읽지 못했다. `hint` 의 사유(gh 로그인, PR 번호, 네트워크)를 확인한다 |

`status` 의 `event` 가 `waiting` 이면 지금 할 일이 없다는 뜻이다.
표시는 `gh` 계정 본인이나 PR 작성자가 올린 코멘트의 것만 읽는다. 남이 쓴 표시는 진행이나 처리 기록으로 치지 않는다.

### 3.1 CI 실패
1. 로그를 본다: `gh run view <checks.failed[].runId> --log-failed | tail -200`. runId 가 없으면 `url` 을 연다.
2. 원인이 이 PR 의 변경이면 고치고 커밋·푸시한다.
3. 이 PR 과 무관하면(인프라, flaky, base 에서도 같은 실패) 코드를 건드리지 않는다. base 의 같은 체크 이력에서 같은 실패를 확인하고
   (`gh run list --branch <base> --workflow <워크플로> --limit 10`, 실패한 실행은 `gh run view <id> --log-failed`),
   실패한 잡만 한 번 다시 돌린다(`gh run rerun <runId> --failed`). 다시 실패하면 근거를 붙여 보고한다.
4. 어느 쪽이든 CI 코멘트(step=ci)를 올린다: 실패한 체크 이름, 원인, 고친 커밋 또는 재실행한 근거. 이 기록으로 횟수를 센다.
5. 고쳐서 푸시했는데도 같은 체크가 세 번 이어서 실패하면 멈추고 보고한다.

고치는 대상은 결함이다. 체크를 통과시키려고 테스트를 skip 하거나 지우거나 기대값을 실패에 맞추지 않는다.
`--admin` 머지와 필수 체크 해제도 하지 않는다.

### 3.2 리뷰어 코멘트
`feedback.items` 를 하나씩 본다(`kind`: `review` 리뷰 본문, `thread` 인라인 스레드, `comment` 대화 코멘트).
지적이 맞는지는 코드로 확인한 뒤에 판단한다.

| 판단 | 할 일 |
|---|---|
| 맞는 지적이고 평가가 잰 동작을 바꾸지 않는다 | 고친다 |
| 질문이다 | 코드를 근거로 답한다 |
| 반영하지 않는 것이 맞다(`안 고침`) | 기술적 근거를 적은 답글 문안을 쓰되 **올리지 않는다**. 사용자가 승인한 뒤에 올린다(5번) |
| 평가가 잰 동작을 바꾼다, 범위나 설계를 바꾼다, 사용자의 결정과 부딪힌다 | 고치지 않고 3.4 로 간다 |

요청된 변경을 전부든 일부든 하지 않은 채 두는 답은 형식이 무엇이든 `안 고침` 이다. 질문처럼 쓴 요청("왜 dict 를 안 쓰나요?")에
"바꾸지 않는다"고 답하는 것도 같다. 한 항목의 요청 중 일부만 고쳤으면 나머지는 `안 고침` 으로 묻는다. 그 항목이 `thread` 면 답글 전체를 미룬다(내 답글이 달리면
감시가 그 스레드를 끝난 것으로 본다). 반영 코멘트에는 `일부 고침 → <커밋>, 나머지 사용자 확인 중` 으로 적고 `ask` 에 넣은 뒤,
승인을 받으면 고친 것과 승인된 문안을 한 답글로 올린다.
요청이 동작을 바꾸더라도 반영하지 않는 것이 맞다고 판단하면 `안 고침` 이다. 3.4 는 반영할 수도 있어 사용자가 정해야 하는 항목에 쓴다.
본문이 없는 `review` 는 인라인 스레드를 실어 온 것이다. 따로 답하지 않고 `until` 로 닫는다.
답글은 리뷰어가 쓴 언어로 쓴다. 아래의 `고침 →` 같은 표기는 반영 코멘트의 형식이고, 답글에서는 같은 내용을 문장으로 써도 된다.

순서(3.4 로 가는 항목이 있으면 3.4 의 1 과 2 를 먼저 한다):
1. 고칠 것을 고치고 테스트한 뒤 커밋·푸시한다.
2. `thread` 마다 답글을 단다(`고침 → <커밋>`, 답, 3.4 로 간 항목은 사실만: 측정은 현재 값 기준이고 변경 여부를 확인 중).
   `안 고침` 으로 판단한 스레드에는 아직 달지 않는다. 내 답글이 마지막인 스레드는 다음 감시에서 처리된 것으로 본다.
   ```bash
   gh api graphql -f query='mutation($t:ID!,$b:String!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$t,body:$b}){comment{url}}}' -f t=<threadId> -f b="<답글>"
   ```
3. 반영 코멘트를 올린다. 표시의 `until` 에 `feedback.until` 을 그대로 넣는다(null 이면 뺀다). 그 시각까지의 리뷰 본문과
   대화 코멘트를 처리했다는 기록이고, 빠지면 같은 항목이 다음 감시에서 다시 온다.
   `ask` 에는 문안 승인을 기다리는 항목의 `id` 를 공백과 따옴표 없이 쉼표로 이어 적는다: 이번에 `안 고침` 으로 판단한 `feedback.items[].id` 와
   `feedback.awaitingUser[].id` 전부(답하지 않기로 한 스레드 포함, 그 `답하지 않음(사용자 결정)` 줄도 새 코멘트에 옮겨 적는다).
   없으면 `ask` 를 뺀다. 감시는 **마지막** 반영 코멘트의 `ask` 만 읽는다
   (`until` 은 모든 반영 코멘트 중 가장 늦은 값을 쓰므로 뒤의 코멘트에서 빠져도 앞의 기록은 남는다).
   ```
   <!-- pr-review-loop step=feedback round=<markers.next.feedback> sha=<head> until=<feedback.until> ask=<id,...> -->
   - <리뷰어> <항목>: 고침 → <커밋> | 답함 | 안 고침 → <이유> | 사용자 확인 중
   ```
   `안 고침 → <이유>` 줄에는 사용자가 승인한 문안만 적는다. 승인 전의 항목은 `사용자 확인 중` 으로 적는다.
4. 재리뷰를 요청한다(`gh pr edit <PR> --add-reviewer <login>`). 대상은 `review.changesRequested`, `review.dismissed`
   (푸시로 승인이 취소된 리뷰어), `mode=manual` 이면 `review.staleApprovals`(이전 커밋을 승인한 리뷰어) 중
   `review.requested` 에 없는 사람이다. 그 리뷰어의 항목에 `사용자 확인 중` 이 남아 있으면 결정이 난 뒤에 요청한다.

5. `안 고침` 항목이 있으면 사용자에게 문안을 보여 묻는다. 항목마다 리뷰어와 링크(`url`), 지적 요약, 반영하지 않는 근거,
   **올릴 문안 그대로**를 적는다. 묻고 나서도 `wait` 는 다시 띄운다. `ask` 에 적힌 항목은 `feedback.awaitingUser` 로 옮겨져
   리뷰어가 덧붙이기 전에는 다시 깨우지 않는다. auto-merge 는 끄지 않는다. 그 리뷰어가 답을 기다리지 않고 승인하면 그 항목은 끝난 것으로 본다.
   답을 받으면 올리기 전에 `status` 로 그 항목이 아직 `feedback.awaitingUser` 에 있는지 본다. `feedback.items` 로 돌아와 있으면
   리뷰어가 덧붙인 것이니 다시 판단한다. 어디에도 없으면 그 사이 상태가 바뀐 것이다(리뷰어가 승인했거나 스레드를 resolve 했거나
   리뷰가 dismiss 됐거나 누군가 직접 답했다). 올리지 않고 원인을 확인해 사용자에게 알린다.
   - **승인**(사용자가 고친 문안 포함): `thread` 는 그 문안을 답글로 올리면 풀린다. `review` 와 `comment` 는 `ask` 에서 그 `id` 를 빼고
     `안 고침 → <승인된 문안 그대로>` 줄을 적은 반영 코멘트를 새로 올려야 풀린다. 그 뒤 4번을 한다.
     `wait` 가 이미 돌고 있으면 그대로 둔다.
   - **고쳐라**: 그 항목으로 1-4번을 다시 한다(동작을 바꾸면 3.4). 새 반영 코멘트의 `ask` 에서 그 `id` 를 뺀다.
   - **답하지 않음**: 새 반영 코멘트에 `답하지 않음(사용자 결정)` 줄로 남긴다. `review` 와 `comment` 는 `ask` 에서 빼면 끝난다.
     `thread` 는 `ask` 에 둔 채(빼면 다시 깨운다) 4번을 한다. 일부를 고친 스레드였으면 고친 부분만 답글로 올린다(거절이 없는 답이고
     스레드도 닫힌다). 이 스레드는 `feedback.awaitingUser` 에 계속 보이지만 다시 묻지 않는다.
     `rules.threadResolutionRequired` 가 true 면 누군가 resolve 하기 전에는 머지되지 않는다고 그 자리에서 사용자에게 알린다.
   - 답이 없으면 올리지 않는다. 무응답은 승인이 아니다.

   이렇게 뒤따라 올리는 반영 코멘트에는, 그 시점의 `feedback.items` 까지 1-4번으로 처리했을 때만 `until` 을 넣는다.
   처리하지 않은 항목이 있는데 `until` 을 넣으면 그 항목이 기록에 덮여 사라진다.

승인은 사람이 한다. 스스로 approve 하거나 남의 리뷰를 dismiss 하지 않는다. 스레드 resolve 는 리뷰어에게 맡긴다.
`rules.threadResolutionRequired` 가 true 이고 그 리뷰어가 승인한 뒤라면, 고치고 답글을 단 스레드만 resolve 한다.
`gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -f t=<threadId>`

### 3.3 base 와 어긋남
- `behind`: `gh pr update-branch <PR>` (base 를 merge 한다). rebase 와 force-push 는 리뷰 기록을 깨므로 쓰지 않는다.
- `conflict`: 로컬에서 base 를 merge 해 충돌을 풀고, 테스트한 뒤 푸시한다. 충돌 해소가 평가가 잰 코드에 닿으면 3.4 로 간다.

### 3.4 진행을 시작한 뒤 코드 변경의 한계
절차가 확인한 것은 기준 커밋(`markers.acceptedSha`)의 동작이다. 그 뒤의 수정(CI 수정, 리뷰 반영, 충돌 해소)이 평가가 잰 동작
(평가가 없었으면 리뷰어가 본 동작)을 바꾸면 머지를 진행할 근거가 사라진다.

- 동작을 바꾸지 않는 수정(테스트, lint, 타입, 주석과 문서, 이름 변경, 측정 경로 밖의 코드)은 그대로 진행한다.
- 동작을 바꾸는 수정(알고리즘, 기본값과 설정, 측정 경로의 분기)이 필요하거나 요청되면 아래 순서로 멈춘다.
- 어느 쪽인지 애매하면 `git diff <acceptedSha>..HEAD` 로 확인하고, 그래도 애매하면 바꾸는 쪽으로 본다.

멈추는 순서:
1. auto-merge 가 켜져 있으면 먼저 끈다: `gh pr merge <PR> --disable-auto`. 켜 둔 채 물으면 답이 오기 전에 승인만으로 머지된다.
2. hold 코멘트를 올린다(형식은 2단계와 같다). 리뷰 스레드에서 나온 질문이면 그 `threadId` 를 본문에 적어 둔다.
3. 사용자에게 묻는다: 바꾸지 않음 / 반영하고 재평가 / 후속 PR. 리뷰어의 요청에서 나온 것이면 바꾸지 않음과 후속 PR 을 골랐을 때
   리뷰어에게 올릴 답글 문안도 그대로 함께 보인다. 이 답글도 `안 고침` 이다.
4. 답에 따라:
   - **바꾸지 않음, 후속 PR**: 선택만 받고 문안 승인을 못 받았으면 문안을 다시 보여 승인받는다. 그때까지 보류를 닫지 않는다
     (open 코멘트도 auto-merge 도 없다). 승인된 문안 그대로 해당 스레드에 답한 뒤에 2단계 표대로 auto-merge 를 다시 켠다.
     open 코멘트(`sha=<지금 head>`, 본문은 `재개: <결정>`)를 올려 보류를 닫고, 3.2 의 4번을 한 뒤 3단계로 돌아간다.
   - **반영하고 재평가**: 먼저 `평가 무효: 커밋 <옛 평가 커밋>, 사용자 지시 재평가.` 코멘트(step=poc)를 올리고(평가가 없던 PR 이면
     커밋 자리에 기준 커밋을 적는다) pr-review-loop 2단계부터
     다시 한다(수정, 리뷰, CI 와 평가, 머지 진행 판정). 리뷰 라운드 상한은 새로 7회다. 그 7단계의 4 가 다시 차면 1단계부터 한다.

## 4. 끝
`merged` 이벤트가 오거나 직접 머지가 성공하면 사용자에게 보고하고 끝낸다: 머지 커밋(`pr.mergeCommit`), 기준 커밋 뒤에
추가된 커밋, 처리한 CI 실패와 리뷰 반영. `feedback.awaitingUser` 가 남아 있으면 보고에 넣고, 사용자가 승인한 문안은 머지 뒤에도 올린다.
로컬 브랜치와 워크트리를 정리한다(squash 머지 뒤에는 `git branch -D`).
원격 브랜치는 저장소의 자동 삭제 설정에 맡긴다.

## 멈추고 사용자에게 보고할 때
프로젝트 규칙이 요구하는 허락(릴리스 브랜치 머지 등), 리뷰할 사람이 정해지지 않은 남의 저장소(2단계의 4), 3.4 의 동작 변경, 같은 체크의 3회 실패, PR 과 무관한 실패의 재실패,
`mode=unreviewed` 에서 돌릴 CI 가 없는 `ci_not_started`, 남이 끈 `auto_merge_off`, 남이 되돌린 `draft`, 직접 머지의 실패, 원인을 못 찾은 `stuck`, `closed`.
보고에는 PR 링크, 지금 상태(`hint`), 선택지를 넣는다.
3.2 의 `안 고침` 문안은 사용자에게 묻되 감시는 멈추지 않는다.

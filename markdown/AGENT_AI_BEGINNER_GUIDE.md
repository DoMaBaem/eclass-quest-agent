# E-Class Quest Agent AI 입문 가이드

## 1. 무엇을 만드는가

E-Class Quest는 TUI가 실행되는 동안 E-Class를 확인하고, 중요한 공지·과제·강의 상태를 먼저
알려 주는 LMS 비서다. 사용자가 자연어로 요청하면 필요한 웹 조회나 문서 분석도 수행한다.

이 프로젝트에서 “AI가 알아서 한다”는 말은 모델에게 프로그램 전체 권한을 준다는 뜻이 아니다.
AI는 의미를 판단하고, Python 코드가 정해진 절차와 안전 규칙대로 실제 작업을 실행한다.

---

## 2. 전체 구조를 회사에 비유하기

```text
사용자
  ↓
Manager Agent                    요청·문맥·응답 정책을 소유하는 담당 비서
  ↓ typed plan
Python Runtime                   업무 순서와 권한을 강제하는 운영 규칙
  ├─ E-Class Agent              LMS 업무 전문 직원
  │    └─ E-Class MCP           LMS 전용 업무 창구
  └─ Document Agent             첨부문서 분석 전문 직원
       ├─ MarkItDown MCP        문서를 Markdown으로 바꾸는 창구
       └─ Qwen Tool             변환된 문서를 분석하는 도구

MySQL                           확인한 사실과 변경 이력을 보관하는 장부
Playwright                      웹 브라우저를 실제로 누르는 손
Textual TUI                     사용자에게 보여 주는 화면
```

Agent는 총 3개다. Agent 수를 줄인 이유는 일을 줄이기 위해서가 아니라, 같은 요청을 여러 모델이
반복해서 해석하며 이름·번호·문맥을 바꾸는 오류 지점을 줄이기 위해서다.

---

## 3. Agent, Tool, MCP의 차이

| 개념 | 쉬운 비유 | 하는 일 | AI 모델인가? |
|---|---|---|---|
| Manager Agent | 담당 비서 | 요청 이해, 전문 작업 선택, CHAT 답변 | 예 |
| E-Class Agent | LMS 전문 직원 | 필요한 E-Class 업무와 Tool 선택 | 예 |
| Document Agent | 문서 전문 직원 | 문서 변환·분석 과정 판단 | 예 |
| Tool | 한 가지 기능 | 조회, 재생, 분석 같은 제한된 동작 | 아니오 |
| MCP Server | 표준화된 기능 창구 | Tool 목록과 구조화 결과 제공 | 아니오 |
| Playwright | 브라우저 조작기 | 클릭, 입력, 페이지 읽기 | 아니오 |
| Runtime | 실행 관리자 | 순서, 한도, 상태, 오류, 취소 통제 | 아니오 |

Agent가 브라우저 화면을 마음대로 클릭하는 것이 아니다.

```text
Agent가 “2주차 강의 조회”라는 고수준 Tool을 선택
→ MCP가 과목과 강의를 확정
→ Playwright가 실제 E-Class 화면 조작
→ MCP가 구조화된 결과 반환
→ Agent가 검증된 결과만 설명
```

---

## 4. 세 Agent의 역할

### 4.1 Manager Agent

Manager는 사용자와 계속 대화하는 유일한 최상위 Agent다.

- “빅데프 2주차 영상 틀어줘” 같은 자연어 요청을 이해한다.
- E-Class 작업인지 문서 작업인지 결정한다.
- 사용자가 말한 과목, 주차, 기간과 조건을 보존한다.
- CHAT은 직접 답하고, 전문 작업 결과는 Runtime이 원문 보존 형식으로 표시하도록 범위를 소유한다.
- 여러 후보가 있으면 실제 후보를 보여 주고 다시 묻는다.
- TUI가 켜진 동안 안전하게 정리한 대화 문맥을 유지한다.

Manager는 LMS에 직접 들어가지 않으며 과목 ID나 강의 ID를 추측하지 않는다.

### 4.2 E-Class Agent

E-Class Agent는 LMS 업무만 다룬다.

- 강좌·공지·과제·강의·성적 조회
- 제출·시청·출석 상태 확인
- 사용자가 요청한 영상 재생·중지
- E-Class MCP의 고수준 Tool 사용

HTML, CSS 선택자, 쿠키와 비밀번호는 보지 않는다. 과목과 강의 후보를 확정하는 작업은 MCP 내부의
resolver가 담당한다.

### 4.3 Document Agent

Document Agent는 이미 E-Class에서 검증해 내려받은 첨부파일만 분석한다.

```text
download_id
→ MarkItDown MCP
→ Markdown
→ Ollama의 qwen3:0.6b Tool
→ 요약·제출 조건·평가 기준·할 일
```

`파일들 내용 알려줘`처럼 복수형이면 Runtime이 같은 과제의 첨부인지 확인한 뒤 최대 5개를 순서대로
처리한다. 파일마다 위 과정을 반복하고 결과에는 원래 파일명을 붙인다.

Qwen은 네 번째 Agent가 아니라 Document Agent가 사용하는 Tool이다.

---

## 5. Agent가 아닌 것

다음 기능은 AI의 창의적인 판단이 필요하지 않으므로 Python 코드로 구현한다.

| 기능 | 구현 형태 | 이유 |
|---|---|---|
| Heartbeat | Textual Timer + SyncService | 정해진 시간마다 실행하면 됨 |
| Checklist | 계산 서비스 | 강의·과제 상태를 집계하면 됨 |
| Deadline | 조건식 | 마감까지 남은 시간을 계산하면 됨 |
| Mission | Service + Repository | 생성·중복 방지·완료 규칙이 확정적임 |
| ID resolver | MCP 내부 코드 | 정확한 ID만 다음 단계로 보내야 함 |
| Guardrail | 검사 코드 | Agent 판단과 관계없이 항상 적용해야 함 |
| TUI | Textual 화면 코드 | 결과를 표시하는 계층임 |

과거 문서와 코드에는 `Mission Agent` 또는 `Checklist Agent`라는 이름이 있었지만 목표 구조에서는
`MissionService`로 정리한다. 일정의 중요성을 사용자에게 설명하는 역할은 Manager가 맡는다.

---

## 6. Custom orchestration은 무엇인가

Manager는 사용자의 의도와 범위를 typed plan으로 만들고, Python Runtime이 그 계획을 검사한 뒤
E-Class 또는 Document Agent를 필요한 경우에만 명시적으로 호출한다.

```text
사용자 → Manager 계획 → Runtime → 전문 Agent → Runtime 검증·조합 → 사용자
```

각 작업은 자유 형식 설명만으로 전달하지 않고 다음 최소 계약을 가진다.

```text
entity: ASSIGNMENT | ANNOUNCEMENT | LECTURE | ...
action: LIST | DETAIL | PLAY | PREVIEW | ...
slots:  year, semester, course_query, query, week, ordinal, filter
```

Manager가 사용자 문맥과 작업 의미를 계속 소유하므로 사용자는 Agent가 바뀔 때마다 설명을 반복할
필요가 없다. Runtime은 전문 결과의 상태와 근거를 검사하고, 고유명사·숫자·URL이 다시 LLM을 거치며
변형되지 않도록 결정적으로 표시 결과를 조합한다.

현재 구조는 SDK handoff나 `agent.as_tool()`에 Agent 선택을 맡기지 않는다. 명시적인 Python 호출을
사용해 실행 순서, 최대 단계, 허용 Tool과 실패 처리를 테스트할 수 있게 한다. Runtime과 E-Class
handler는 `entity + action`별 Operation Policy로 필요한 MCP Tool만 노출하고, 요청 종류와 맞는 결과만
최종 결과로 채택한다.

---

## 7. 실제 요청이 처리되는 과정

### 과제 조회

```text
사용자: “2026년 1학기 빅데프 과제 알려줘”
1. Manager가 과목·학기·과제 조회 의도를 구조화한다.
2. Runtime이 과제 목록 Operation Policy를 선택한다.
3. MCP resolver가 실제 과목 후보를 확정한다.
4. Playwright가 해당 과목의 과제를 읽는다.
5. 검증된 과제 목록을 문맥에 저장한다.
6. Runtime이 원문 보존 형식으로 목록을 표시한다.
```

### 후속 요청

```text
사용자: “첫 번째 자세히 알려줘”
1. Manager는 직전 목록의 첫 항목을 뜻한다고 해석한다.
2. Python 문맥 저장소가 실제 assignment_id를 꺼낸다.
3. MCP가 그 ID의 상세 페이지를 조회한다.
4. Runtime이 검증된 본문과 첨부파일을 표시한다.
```

Agent가 번호를 기억해 임의의 ID를 만드는 방식이 아니다.

### 영상 재생

```text
사용자: “빅데프 2주차 영상 틀어줘”
→ Manager가 과목·주차·재생 의도를 추출
→ resolver가 실제 강의 후보 확인
→ 하나면 검증된 lecture reference 생성
→ 여러 개면 사용자에게 제목 선택 요청
→ 명시적 재생 요청 Guardrail 통과
→ Playwright headed 창에서 재생
→ Runtime이 실제 재생 결과를 검증해 표시
```

---

## 8. 능동 알림은 Agent가 시간을 세는가

아니다. Textual Timer와 Python 서비스가 시간을 관리한다.

```text
TUI 시작 또는 30분 Timer
→ E-Class 최신 상태 수집
→ MySQL의 이전 상태와 비교
→ 새 공지·과제·미시청 강의와 마감 계산
├─ 중요한 변화 없음: 시각만 갱신
└─ 중요한 변화 있음: 구조화 이벤트를 Manager에 전달
                         → Manager가 사용자보다 먼저 알림
```

TUI를 종료하면 Timer도 종료되므로 꺼져 있는 동안에는 알림을 보내지 않는다.

---

## 9. Guardrail

Guardrail은 Agent의 성격이나 프롬프트 한 문장이 아니라 코드로 강제하는 안전 규칙이다.

- Input Guardrail: 비밀번호·쿠키·토큰 마스킹, 입력 길이와 형식 검사
- Workflow Policy: 허용된 작업 순서와 최대 단계 검사
- Tool Guardrail: E-Class URL, 파일 경로, 학기와 검증 참조 검사
- Result Validator: 실제 Tool 성공과 근거 ID 검사
- Output Guardrail: 비밀값과 확인하지 않은 성공 표현 제거

Manager가 실수하더라도 Tool 경계에서 위험한 인자가 차단돼야 한다.

Agents SDK trace에는 `trace_include_sensitive_data=False`를 적용해 모델 입력과 Tool 인자·결과 원문을
포함하지 않는다. 단계·상태·오류 코드는 별도의 Audit으로 확인한다.

---

## 10. 프롬프트보다 먼저 고칠 것

시스템 프롬프트는 중요하지만 모든 오류를 해결하지는 못한다.

```text
프롬프트에 “course_id와 lecture_id를 혼동하지 마라” 추가
```

보다 다음 구조가 더 안정적이다.

```text
resolve_lecture(course_query, week, title_query)
→ verified lecture reference
→ play_resolved_lecture(reference)
```

상용 서비스의 긴 프롬프트를 복사하기보다 이 프로젝트의 Tool 입력·출력과 실패 상태를 명확히 하고,
프롬프트에는 역할·범위·모호성 처리만 남긴다.

---

## 11. 현재 상태와 다음 순서

현재 핵심 구조 보정은 코드에 반영됐다.

- Agent를 Manager·E-Class·Document 3개로 제한했다.
- Mission은 LLM 없는 `MissionServiceHandler`가 처리한다.
- Manager의 Agent Tool·handoff와 별도 합성 LLM 호출을 제거했다.
- Manager 작업은 `entity + action + slots` 계약을 사용하고 Runtime이 종류에 맞는 Snapshot만 연결한다.
- Runtime이 E-Class·Document specialist를 명시적으로 호출하고 검증 결과를 결정적으로 조합한다.
- E-Class 작업은 Operation Policy가 허용 Tool과 기대 결과를 제한한다.
- Document Agent가 실제 Runner로 검증 문서 Tool을 호출한다.
- 과목명·주차 중심 업무 Tool 6개와 기본 학기 일괄 동기화 `get_dashboard_snapshot()`을 추가했다.
- 재생·미리보기에는 원시 강의 ID 대신 만료되는 불투명 검증 참조를 사용한다.
- 과목·공지·과제·강의·첨부별 검증 Snapshot과 민감 payload를 제외한 요청 단위 trace를 추가했다.
- 자동화 회귀 테스트는 계속 통과하도록 유지한다.

남은 핵심 검증은 실제 E-Class에서 공지 상세, 특정 과제 후속 요청, 여러 강의의 모호성 처리와
검증 참조 재생을 연속 대화로 확인하는 smoke test다. Dashboard 전용 단일 Tool은 구현되어
Startup·Heartbeat가 같은 서비스 계약을 사용한다.

구현 여부는 [`ROADMAP.md`](./ROADMAP.md), 상세 설계는
[`Architecture.md`](./Architecture.md)에서 확인한다.

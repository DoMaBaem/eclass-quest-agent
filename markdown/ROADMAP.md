# E-Class Quest 구현 로드맵

> 전체 개념이 낯설다면 [`AGENT_AI_BEGINNER_GUIDE.md`](./AGENT_AI_BEGINNER_GUIDE.md)를 먼저 읽는다.

## 목표

E-Class Quest는 한성대학교 E-Class의 공지·과제·강의·문서를 조회하고, **TUI가 실행되는 동안
중요한 일정과 변경 사항을 먼저 알려 주는 능동형 LMS 비서**다.

```text
사용자 요청 또는 TUI Runtime 이벤트
→ LMS Manager Agent
→ Runtime이 typed plan 검증
→ 필요한 E-Class / Document 전문 Agent 명시적 호출
→ MCP·Tool·결정적 Python Service 실행
→ 검증된 결과·미션·능동 알림
```

별도 Gateway나 24시간 백그라운드 서비스는 만들지 않는다. TUI가 실행되는 동안에만 시작 동기화,
주기 동기화, 마감 검사를 수행하며 TUI가 종료되면 모두 중지한다.

---

## 구현 원칙

- 실제 LMS만 사용하고 Mock 경로를 만들지 않는다.
- E-Class MCP 서버와 Playwright Adapter를 직접 작성한다.
- 개인 개발 환경에서는 `.env` 자격증명으로 세션 만료 시 자동 재로그인하고, 운영에서는 같은 값을
  Secret Manager로 주입한다. 자격증명은 DB·로그·Agent·MCP 결과에 전달하지 않는다.
- `LMS Manager Agent`가 사용자 문맥·작업 범위·응답 정책을 소유한다.
- Manager는 각 작업을 `entity + action + slots`로 구조화하고, Runtime은 이 typed plan을 검증한 뒤
  E-Class 또는 Document Agent를 명시적으로 호출한다.
- SDK handoff와 `agent.as_tool()` 등록에 실행 순서를 맡기지 않는다.
- 검증된 전문 결과는 별도 합성 LLM 없이 Runtime이 결정적으로 조합한다.
- `entity + action`별 Operation Policy로 E-Class Agent에 노출할 Tool과 기대 결과를 제한한다.
- 주기적인 수집과 Snapshot 비교에는 LLM을 사용하지 않는다.
- 변경이 있을 때만 Manager Agent를 호출해 API 비용을 줄인다.
- Manager 프롬프트와 별도로 Input·Tool·Output Guardrail을 코드로 강제한다.
- 조회와 로컬 미션 생성은 자동으로 허용한다.
- 영상 재생, 첨부파일 다운로드와 분석은 사용자의 명시적인 요청이 있을 때만 수행한다.
- 과제 자동 제출·삭제와 자동 영상 재생을 통한 출석 취득은 구현하지 않는다.

---

## 목표 Agent 구조: 총 3개

```text
LMS Manager Agent
├─ Runtime 호출 → E-Class Agent
└─ Runtime 호출 → Document Analysis Agent
```

| Agent | 책임 |
|---|---|
| LMS Manager Agent | 요청·이벤트 판단, typed 계획, 사용자 문맥, CHAT 응답과 능동 알림 |
| E-Class Agent | 강좌·공지·과제·강의·성적 조회와 사용자 요청 영상 제어 |
| Document Analysis Agent | MarkItDown 변환과 Ollama/Qwen 문서 분석 |

Agent가 아닌 결정적 Python 서비스는 다음과 같다.

| 서비스 | 책임 |
|---|---|
| Heartbeat·Sync | 주기 수집, Snapshot 비교, 구조화 이벤트 생성 |
| Checklist·Deadline | 열린 강의·이번 주 과제 집계, 마감 임박 계산 |
| Mission Service | 미션 생성·중복 방지·조회·완료, 알림 중복 관리 |
| ID resolver | 과목·공지·과제·강의 후보와 실제 ID 확정 |
| Guardrail·Approval | 입력·Tool·출력·권한 검사 |
| TUI·Trace | 화면 표시와 실행 관측 |

기존 `System Companion`, `Triage`, `Checklist`, `Mission Agent`는 목표 Agent 목록에 포함하지 않는다.
과거 완료 체크는 당시 구현 기록이며, 8.5단계에서 3-Agent 구조로 바로잡는다.

---

## 전체 구현 순서

```text
0. 개발 기반과 MySQL                         완료
1. 기존 대화형 Agent 프로토타입               완료 후 Manager 입력 경로로 교체
2. 공통 데이터 계약과 MySQL 저장소             완료
3. Playwright 로그인·강좌 탐색                 완료
4. Manager 중심 Agent Runtime 리팩터링         완료
5. E-Class MCP 읽기 Tool                      완료
6. TUI 능동 동기화와 이벤트 Runtime            완료
7. 영상·첨부문서·Mission 저장 기능             완료
8. Guardrail과 전체 Agent/MCP 통합             완료
8.5 3-Agent 실행 계약과 MCP 경계 안정화          핵심 코드 완료·실제 LMS 검증 남음
9. 테스트·패키징·배포                          완료
```

---

## 0. 개발 기반과 개발용 MySQL

### 완료 항목

- [x] Git 저장소 생성
- [x] Python 가상환경 생성
- [x] `requirements.txt` 작성
- [x] `.env.example`, `.gitignore` 작성
- [x] Docker Compose로 개발용 MySQL 8.0 실행
- [x] Playwright Chromium 설치
- [x] Ollama와 `qwen3:0.6b` 설치 확인
- [x] Textual 설치
- [x] `app/`, `mcp_server/`, `scripts/`, `tests/` 기본 구조 생성
- [x] OpenAI Agents SDK와 `gpt-5.6-terra` 설정
- [x] Windows용 `run.ps1`·`run.cmd`, macOS·Linux용 `run.sh` 제공
- [x] 모든 OS가 공유하는 `scripts/local_launcher.py` 구현 및 실행 순서 테스트

### 최초 실행 설정과 로컬 연결 환경변수

모델·OpenAI API 키·E-Class 계정은 OS별 실행 파일의 최초 실행에서 입력한다. 모델은 일반 설정
파일, API 키와 계정은 Git에서 제외된 암호화 파일에 보관하며 실행 명령에 `--setup`을 붙여
변경한다. Windows의 `run.ps1`·`run.cmd`와 macOS·Linux의 `run.sh`는 공통 Python 런처를 통해
로컬 MySQL URL 설정, Docker MySQL 시작·health 확인·Alembic migration을 수행한다. 일반 사용자는
`.env`나 `docker compose` 명령을 직접 입력하지 않는다.

```env
# 아래 값은 기본값과 다른 서비스를 사용할 때만 선택적으로 지정한다.
MYSQL_URL=
OLLAMA_URL=http://localhost:11434/api/chat
ECLASS_BASE_URL=https://learn.hansung.ac.kr
ECLASS_STORAGE_STATE_ENCRYPTED=data/sessions/eclass_state.enc
ECLASS_SESSION_ENCRYPTION_KEY=
ECLASS_SYNC_INTERVAL_MINUTES=30
```

`ECLASS_SYNC_INTERVAL_MINUTES`는 TUI가 켜져 있는 동안 실행할 자동 동기화 간격이다.
자동 재로그인은 계정이 저장되어 있으면 항상 사용하고, 다운로드 보존 시간은 24시간으로 고정한다.

---

## 1. 기존 대화형 Agent 프로토타입

이 단계는 현재 코드의 동작을 기록한다. 목표 구조에서는 4단계에서 교체한다.

### 완료 항목

- [x] Textual 기반 `EclassQuestApp` 구현
- [x] System Companion Agent 구현
- [x] CHAT/TASK 구조화 출력 구현
- [x] System Companion 응답 스트리밍 구현
- [x] 안전한 대화 요약 유지
- [x] TUI 실행 중 대화·전문 작업 결과·능동 알림을 하나의 스크롤 기록으로 누적
- [x] Manager에 마스킹된 최근 12개 대화와 누적 요약을 전달하고 종료 시 폐기
- [x] Triage Agent의 목적 코드 분류 구현
- [x] E-Class/Document/Checklist Agent 뼈대 구현
- [x] `WorkflowRunner`의 요청 상태와 단계 제한 구현
- [x] 오류 코드를 TUI 결과로 변환
- [x] 첫 화면과 일반 대화 화면 디자인 통일
- [x] 시스템 창 밖을 터미널 기본 배경색으로 변경

### Manager 구조 교체 현황

- [x] `System Companion Agent` 제거
- [x] `Triage Agent` 제거
- [x] 사용자 입력 실행기를 `ProactiveAssistantRuntime`으로 교체
- [x] 당시 `Checklist Agent`를 결정적 Repository Handler로 교체

이 Handler를 `Mission Agent`라고 부른 것은 과도한 명명이다. 최종 구조에서는 LLM Agent 정의와
전문 Agent 목록에서 제거하고 `MissionService`로 명확히 구분한다.

시스템 이벤트 계약은 4단계에서 반영했다. 실제 Startup·Timer·수동 동기화는 6단계에서 연결한다.

---

## 2. 공통 데이터 계약과 MySQL 저장소

### 완료 항목

- [x] `Course`
- [x] `Assignment`
- [x] `Lecture`
- [x] `Announcement`
- [x] `Attachment`
- [x] `Grade`
- [x] `ToolResult`
- [x] `EclassCollectionResult`
- [x] `DocumentAnalysisResult`
- [x] `MissionResult`
- [x] SQLAlchemy 비동기 MySQL 연결
- [x] Alembic migration 구성
- [x] Snapshot fingerprint 및 변경 이벤트 저장 구조
- [x] 사용자별 브라우저 작업 잠금
- [x] 다운로드 파일 보존 기한 구조

### 추가할 테이블·필드

| 테이블 | 목적 |
|---|---|
| `missions` | 시스템이 생성한 미션과 완료 상태 |
| `notification_history` | 24시간·6시간·1시간 알림 중복 방지 |
| `sync_history` | 동기화 실행·성공·실패와 마지막 동기화 시각 |
| `workflow_runs` | Manager와 전문 Agent 단계·결과 실행 기록 |

- [x] 기존 모델과 migration에 필요한 추가 테이블·필드 반영
- [x] `change_events`에서 대기 중인 Manager 입력용 `ManagerInputEvent` 생성
- [x] 처리 완료 이벤트의 재전달 방지 상태와 요청 ID 기록
- [x] 아이디·비밀번호·쿠키·토큰·평문 세션이 저장되지 않는지 검증
- [x] 기존 개발 DB를 Alembic `20260720_0004`까지 migration
- [x] 새 빈 DB에서 0001부터 0004까지 전체 migration 재현
- [x] ORM과 실제 MySQL 스키마 차이 없음 확인

### 완료 조건

- LMS 최신 상태와 변경 이력을 사용자별로 저장할 수 있다.
- 처리되지 않은 변경만 구조화해 Manager 입력으로 꺼낼 수 있다.
- 처리한 이벤트는 다시 전달되지 않도록 완료 상태를 기록할 수 있다.
- DB에는 로그인 ID·비밀번호·쿠키·토큰·평문 storage state를 저장하지 않는다.

---

## 3. Playwright 로그인과 강좌 탐색

### 완료 항목

- [x] `scripts/login.sh`가 headed Chromium 실행
- [x] 사용자가 브라우저에서 직접 로그인
- [x] Playwright storage state 암호화 저장
- [x] 암호화 키 파일과 세션 파일 권한 설정
- [x] 메모리에서 storage state 복호화
- [x] 로그인 페이지 복귀 시 `AUTH_REQUIRED` 판정
- [x] 연도·학기 필터 기반 강좌 추출 Adapter 작성

### 실제 E-Class 검증 완료

- [x] `scripts/verify_session.py --year 2026 --semester 1`로 실제 강좌 JSON 검증
- [x] 한국어·영어 로그인 완료 표식과 `/login/logout.php` 로그아웃 선택자 확정
- [x] 정규 수강 강좌 `/local/ubion/user/` 메뉴와 연도·학기 query 구조 확정
- [x] 실제 값 `10`·`20`·`15`·`25`를 서비스 학기 `1`·`2`·`3`·`4`에 매핑
- [x] `.my-course-lists a.coursefullname` 강좌 선택자 확정 및 배너 링크 제외
- [x] 과제 `/mod/assign/`, 강의 `/mod/vod/`, 공지 `/mod/ubboard/`, 성적 `/grade/report/user/` 선택자 확정
- [x] 세션 만료 시 `AUTH_REQUIRED` 확인 → `scripts/login.sh` 재로그인 → 같은 조회 재개 검증
- [x] 최초 실행에서 E-Class 계정을 입력받아 암호화 로컬 저장소에 보관
- [x] 세션 만료 시 자격증명 자동 로그인과 암호화 storage state 갱신
- [x] 세션 갱신 성공 후 실패했던 LMS 작업을 정확히 한 번 자동 재시도
- [x] 자동 로그인 실패 시 기존 암호화 세션 보존 및 직접 로그인 안내
- [x] `python -m scripts.refresh_session` 자동 로그인 단독 점검 명령 제공

### 완료 조건

저장된 암호화 세션으로 실제 E-Class에 접속하고 강좌 하나 이상의 `id`, `name`, `url`을 구조화
결과로 반환한다.

---

## 4. Manager 중심 Agent Runtime 리팩터링

파일별 제거·추가·수정 순서와 MCP 착수 전 승인 조건은
[`PRE_MCP_REFACTOR_PLAN.md`](./PRE_MCP_REFACTOR_PLAN.md)를 따른다.

> 이 단계는 System Companion·Triage를 제거하고 Manager Runtime 기반을 만든 당시 완료 기록이다.
> 이후 실제 사용에서 `agent.as_tool()` 선언, Runtime 수동 분배, Mission Agent 명칭과 별도 합성 호출이
> 한 경로에 섞인 문제가 확인됐다. 최종 실행 계약은 8.5단계에서 교정한다.

### 새 파일 구조

```text
app/
├─ runtime/
│  ├─ assistant_runtime.py
│  ├─ event_queue.py
│  └─ events.py
├─ agent/
│  ├─ manager_agent.py
│  ├─ eclass_agent.py
│  └─ document_agent.py
└─ schemas/
   ├─ runtime.py
   └─ manager.py
```

### 구현 항목

- [x] `LMS Manager Agent` 시스템 프롬프트와 사용자 요청용 구조화 출력 정의
- [x] Manager가 사용자 요청과 시스템 이벤트를 모두 처리하도록 구현
- [x] `USER_REQUEST`, `STARTUP_BRIEFING`, `LMS_CHANGED` 이벤트 모델 정의
- [x] `DEADLINE_WARNING`, `ATTENDANCE_WARNING`, `SESSION_EXPIRED` 이벤트 모델 정의
- [x] 이전 구현에서 E-Class·Document·Mission 정의를 `agent.as_tool()`로 등록
- [x] 이전 구현에서 별도 Manager 호출로 전문 결과를 합성
- [x] 이전 구현에 선택적 handoff 주입 지점 추가
- [x] 한 실행의 Agent·Tool 4단계 제한, 중복 단계·잘못된 의존 순서 차단
- [x] 시스템 이벤트에는 사용자 원문 대화 대신 구조화 데이터만 전달
- [x] `ManagerResult`, `ManagerPlan`, `AssistantContext`, 중복 방지 Event Queue 구현
- [x] TUI를 `ManagerResult` 분기로 전환하고 능동 알림 진입점·종료 처리 추가
- [x] 실제 Tool 미연결 전문 Agent는 가짜 결과 없이 `CAPABILITY_NOT_READY` 반환
- [x] 사용자 LMS 조회 요청을 `TASK`로 계획해 E-Class Agent에 배정
- [x] E-Class Agent 실행 동안 로컬 MCP stdio 서버를 연결하고 종료 시 정리
- [x] 직전 전문 작업 범위·검증 결과를 `AssistantContext`에 보존해 생략형 후속 요청 연결
- [x] 공지 목록의 번호·ID·제목·URL을 MCP 원본으로 보존해 상세 후속 요청에 정확히 연결
- [x] 번호로 선택한 공지 상세는 Agent 재검색 없이 검증된 URL로 MCP Tool 직접 호출
- [x] 공지 목록과 상세 본문을 Manager 재작성 없이 검증된 원문으로 TUI에 표시
- [x] 강좌명·담당자를 MCP 원문으로 표시하고 오해 가능한 대괄호 통합 그룹 코드는 화면에서 제외
- [x] 문장 키워드 대신 실제 마지막 MCP Tool을 기준으로 검증된 직접 출력 선택
- [x] Manager·E-Class·결과 통합 프롬프트에 고유명사 한 글자 변형 금지 규칙 적용

### 완료 조건

- “이번 주 과제를 확인해줘”가 Manager → E-Class Agent 경로를 사용한다.
- “과제 문서를 분석해 미션으로 만들어줘”가 Manager 계획 → E-Class → Document → MissionService
  순서로 실행된다.
- 일반 학교 대화에는 전문 Agent와 MCP를 호출하지 않는다.
- 더 이상 System Companion과 Triage를 먼저 실행하지 않는다.

---

## 5. 직접 작성하는 E-Class MCP 읽기 Tool

### 서버 구조

```text
mcp_server/
├─ server.py
├─ browser/
├─ adapters/
├─ parsers/
└─ services/
```

### 구현 Tool

- [x] `check_session()`
- [x] `list_courses(year=None, semester=None)`
- [x] `get_dashboard_snapshot()` — E-Class 기본 학기 5종 현황 일괄 조회
- [x] `list_announcements(course_id=None, limit=20, year=None, semester=None)`
- [x] `get_announcement_details(announcement_url, course_id=None, year=None, semester=None)`
- [x] `list_assignments(days=None, only_incomplete=False, year=None, semester=None)`
- [x] `get_assignment_details(assignment_id, year=None, semester=None)`
- [x] `list_assignment_attachments(assignment_id, year=None, semester=None)`
- [x] `list_lectures(course_id=None, only_unwatched=False, year=None, semester=None)`
- [x] `get_lecture_status(lecture_id, year=None, semester=None)`
- [x] `get_grades(course_id=None, year=None, semester=None)`
- [x] `list_course_announcements(course_query, limit=20, year=None, semester=None)`
- [x] `list_course_assignments(course_query, ..., year=None, semester=None)`
- [x] `list_course_lectures(course_query, week=None, ..., year=None, semester=None)`
- [x] `resolve_lecture(course_query, week=None, title_query=None, ...)`
- [x] `play_resolved_lecture(reference_id, ...)`
- [x] `preview_resolved_lecture(reference_id, ...)`

기존 Tool 15개는 하위 호환성을 위해 유지하고, Agent가 과목 ID·강의 ID를 직접 복사하지 않도록
고수준 Tool 7개를 추가해 총 22개를 제공한다. `get_dashboard_snapshot()`은 첨부 다운로드 없이
기본 학기의 강좌·공지·과제·강의·성적을 all-or-nothing으로 반환한다. `resolve_lecture`는
단일 후보에만 15분 유효 불투명
참조를 발급하며, 재생·미리보기 Tool은 발급되지 않았거나 변조·만료된 참조를 거부한다.

### 공통 규칙

- [x] Agent에 CSS 선택자와 HTML 원문을 노출하지 않음
- [x] 모든 Tool 결과를 Pydantic 모델로 검증
- [x] `AUTH_REQUIRED`, `NOT_FOUND`, `PARSER_CHANGED`, `TEMPORARY_FAILURE` 오류 계약
- [x] 브라우저 context와 탭을 성공·실패 모두에서 정리
- [x] 사용자별 동시 Playwright 작업 잠금
- [x] MCP stdio client로 2026년 1학기 실제 LMS 검증
- [x] Manager → E-Class Agent → MCP 전체 경로로 강좌·공지 실제 조회 검증

연도·학기를 둘 다 생략하면 로그인 후 E-Class가 기본으로 선택한 학기를
그대로 사용한다. 사용자가 둘 다 지정하면 해당 학기 필터를 적용한다. 모든
학기 범위 응답은 `selected_term` 필드로 실제 조회 학기와 선택 출처를 반환한다.
학기 코드 1~4와 함께 `semester_name`(`1학기`, `2학기`, `여름학기`, `겨울학기`)을
반환해 계절학기를 정규학기로 오해하지 않게 한다.
방학 중 기본 학기의 강좌 0개는 오류가 아닌 정상 빈 목록으로 처리한다.

### 완료 조건

강좌, 공지, 과제, 미시청 강의와 제출 상태를 실제 E-Class에서 구조화 결과로 반환한다.

---

## 6. TUI 능동 동기화와 이벤트 Runtime

### 목표 흐름

```text
TUI 시작 / 30분 Timer / 수동 새로고침
→ Textual Background Worker
→ Sync Service
→ E-Class MCP 읽기 Tool
→ MySQL Snapshot 비교
├─ 변경 없음: 동기화 시각만 갱신
└─ 변경 있음: Manager Agent 호출 → 능동 알림
```

### 구현 항목

- [x] `SyncService` 작성
- [x] TUI `on_mount()`에서 시작 동기화 실행
- [x] `set_interval()`로 30분 기본 Heartbeat 등록
- [x] 환경변수로 동기화 간격 변경 지원
- [x] Textual Worker로 UI와 동기화 작업 분리
- [x] 동기화 중복 실행 방지 잠금
- [x] TUI 종료 시 Timer·Worker·browser context 정리
- [x] “지금 다시 확인해”, “이클래스 정보 업데이트해줘” 등의 수동 요청을 즉시 `SyncService`에 연결
- [x] 수동 완료 시 왼쪽 패널·마지막 확인 시각을 갱신하고 실제 Heartbeat Timer도 재설정
- [x] 동기화 중 겹친 수동 요청은 중복 실행 없이 현재 확인 작업의 패널 갱신을 안내
- [x] Snapshot fingerprint 비교와 `change_events` 생성
- [x] 변경이 없으면 OpenAI API를 호출하지 않음
- [x] 변경·마감·중요 시작 브리핑이 있을 때만 구조화 이벤트로 Manager 호출
- [x] 마지막·다음 동기화 시각 표시
- [x] 세션 만료 시 자동 재로그인·세션 갱신 후 동기화 작업 1회 재시도
- [x] 자동 로그인 실패 시 동기화 일시 중지 및 TUI 직접 로그인 안내

### Deadline Service

- [x] 과제 마감 24시간·6시간·1시간 전 검사
- [x] 출석 인정 종료 임박 검사
- [x] `notification_history`로 중복 알림 방지
- [x] 이미 제출·출석 완료한 항목 제외
- [x] 중요 항목이 없으면 TUI에 아무 메시지도 표시하지 않음

학기를 지정하지 않은 능동 동기화는 E-Class가 기본으로 선택한 학기를 사용한다.
첫 수집은 baseline으로만 저장하고, 이후 새로 등장하거나 fingerprint가 바뀐 항목만
`change_events`로 만든다. Manager 처리에 실패한 `PENDING` 변경은 다음 heartbeat에서
재전달한다. 현재 같이 여름학기 강좌가 0개이고 중요 항목이 없으면 선제 메시지 대신
마지막·다음 확인 시각만 갱신한다.

### 완료 조건

사용자가 아무 입력도 하지 않아도 TUI 실행 직후 브리핑이 나타난다. TUI 실행 중 새로운 공지나
과제가 발견되거나 마감이 임박하면 Manager가 먼저 알린다. TUI를 종료하면 동기화도 중지된다.

---

## 7. 영상·첨부문서·Mission 저장 기능

### 7.1 영상 재생

- [x] `play_lecture(lecture_id)` MCP Tool
- [x] `stop_lecture(playback_id)` MCP Tool
- [x] 팝업·새 탭·iframe 처리
- [x] 재생 시작과 종료 상태 검증
- [x] 최대 재생 대기시간과 취소 처리
- [x] `playback_runs` 기록
- [x] 사용자의 명시적 요청 없이 자동 재생하지 않음
- [x] 멘토 시연용 실제 player 5~30초 자동 재생·종료 `preview_lecture`

자동 영상 재생으로 출석을 취득하는 기능은 만들지 않는다.

### 7.2 첨부문서 분석

```text
사용자 한 요청 → 검증된 과제 선택 → E-Class Agent가 과제 첨부 목록 조회
→ Runtime이 단일 첨부 또는 같은 과제의 첨부 묶음 검증
→ 파일별 download_attachment 직접 호출
→ 파일별 MarkItDown MCP → Qwen Tool
→ 파일명별 DocumentAnalysisResult 조합
→ Runtime 또는 MissionService
```

- [x] 안전한 임시 다운로드 경로와 보존 기한
- [x] 원시 `download_attachment`를 Agent에서 숨기고 Runtime 검증 대상만 다운로드
- [x] 과제 상세 조회 결과와 구조화 첨부 목록 Snapshot 연결
- [x] 별도 선행 목록 요청 없이 한 요청에서 `과제 선택 → 목록 → 다운로드 → 분석` 연속 실행
- [x] `파일들·모두·전부·둘 다·각각` 요청에서 같은 과제의 첨부 최대 5개 순차 처리
- [x] 복수 첨부의 부모 과제·다운로드 attachment ID·원래 순서 재검증
- [x] 브라우저 새 탭에서 열리는 `inline` PDF·문서도 원본 응답 바이트로 처리
- [x] 로그인·미리보기 HTML 오저장 방지와 동일 E-Class 원본 URL·파일 시그니처 검증
- [x] 과제 설명용 첨부만 수집하고 학생 제출 파일은 첨부 목록에서 제외
- [x] URL path형과 `pluginfile.php?file=%2F...` query 인코딩형 과제 첨부 링크 모두 지원
- [x] MarkItDown 변환 결과 검증
- [x] Ollama `qwen3:0.6b` Tool 연결
- [x] Qwen 결과 Pydantic 검증
- [x] 변환 실패·낮은 신뢰도 처리
- [x] 보존 기한 검증과 다음 다운로드 정리 시 원본·변환 파일 삭제

현재 자동 탐색 범위는 **과제 상세 화면에 연결된 첨부파일**이다. 일반 강좌 자료실이나 주차별
강의자료를 탐색하는 `list_course_resources()`는 아직 구현하지 않았다. 사용자가 파일 내용·요약·분석을
명시적으로 요청했을 때만 선택 파일 또는 같은 과제의 검증된 첨부 최대 5개를 내려받는다. 과제에
첨부된 `.py`, `.ipynb`, `.zip`도 확장자를 이유로 원본 다운로드에서 제외하지 않지만,
MarkItDown·Qwen 분석 성공 여부는 파일 형식별로 다르며 변환할 수 없는 파일은 이름과 실패 사유를 표시한다.
`Content-Disposition: inline`처럼 클릭 시 브라우저에 바로 열리는 응답도 다운로드 가능한 원본으로 취급한다.
단, HTML 로그인 페이지나 viewer wrapper가 반환되면 그대로 문서로 저장하지 않고 동일 E-Class 호스트의
`pluginfile.php` 원본만 한 번 추적하며, 대표 문서 확장자는 파일 시그니처까지 확인한다.
원본과 변환본은 기본 24시간 동안 `data/downloads/<UUID>/`에 보관되고 다음 정리 실행에서 삭제된다.

### 7.3 Mission Service

아래 기능은 LLM Agent가 아니라 MySQL Repository를 사용하는 결정적 Python 서비스다.

- [x] `create_mission`
- [x] `update_mission`
- [x] `list_today_missions`
- [x] `list_weekly_missions`
- [x] `mark_mission_completed`
- [x] 검증된 마감·출석 상태 기반 Priority 계산
- [x] 동일 과제·강의 미션 중복 방지
- [x] 새로운 미션을 TUI 시스템창으로 먼저 제시

영상 제어 MCP 프로세스는 TUI가 실행되는 동안 유지되어 `playback_id`를 후속 중지 요청에 사용할
수 있고, 종료 시 같은 lifecycle task에서 Playwright와 MCP를 정리한다. 첨부파일은 서버가 발급한
`download_id`로만 MarkItDown MCP에 전달하며 실제 경로는 Agent와 TUI에 공개하지 않는다.

---

## 8. Guardrail과 전체 통합

### Input Guardrail

- [x] 학번·비밀번호·쿠키·토큰 탐지 및 실행 문맥에서 제거
- [x] 입력 길이와 이벤트 형식 검증
- [x] 허용하지 않는 요청 차단

### Tool Guardrail

- [x] E-Class 기본 URL 밖으로 이동 차단
- [x] Tool 인자와 사용자·강좌 범위 검증
- [x] 다운로드 경로 containment 검증
- [x] 조회·행동 Tool 권한 분리
- [x] 각 전문 Agent에 필요한 Tool만 노출

### Human Approval

- [x] 현재 구현하지 않는 제출·삭제 Tool은 등록하지 않음
- [x] 향후 상태 변경 Tool을 추가하면 `needs_approval=True` 적용
- [x] 승인 중단 상태를 저장하고 동일 run으로 재개

### Output Guardrail

- [x] 조회하지 않은 LMS 사실을 성공으로 표현하지 않음
- [x] 비밀값·내부 경로·원본 HTML 제거
- [x] 사용자 출력 Pydantic 구조 검증
- [x] Agent·Tool 실패를 안전한 오류 코드로 변환

### 통합 완료 조건

- [x] 사용자 요청과 능동 이벤트 모두 같은 Manager Runtime을 사용
- [x] Agent 실행, Tool 호출, Guardrail과 오류 상태를 추적 가능
- [x] 이전 구현에서 전문 Agent 결과를 별도 Manager 호출로 통합
- [x] 민감정보가 Agent context, DB, 로그, TUI에 노출되지 않음

별도 합성 호출 제거와 한 요청 전체 trace는 8.5단계 완료 조건이다.

로컬 실행 추적은 `data/audit/workflow.jsonl`에 원문·Tool 인자 없이 상태만 남긴다.
승인 대기 payload도 인증정보 키를 거부하며 `pending_approvals`에서 같은
`request_id`로만 재개한다.

---

## 8.5 3-Agent 실행 계약과 MCP 경계 안정화

이 단계는 기능을 새로 늘리기보다 실제 사용 중 확인된 이름·ID·후속 문맥 오류의 원인을 제거한다.
기존 단위 테스트가 통과한다는 사실과 실제 다중 턴 대화가 정확하다는 사실을 구분한다.

### Agent와 Runtime

- [x] Agent 정의를 Manager·E-Class·Document 3개로 제한
- [x] `Mission Agent` factory·Tool 등록을 제거하고 실행 대상을 `Mission Service`로 명시
- [x] 기존 Mission Handler·Repository 기능을 LLM 없는 `MissionServiceHandler`로 책임 정리
- [x] Manager는 사용자 입력을 `entity + action + slots` typed plan으로 변환
- [x] Runtime이 plan을 검증하고 E-Class 또는 Document Agent를 명시적으로 호출
- [x] Runtime·E-Class handler가 `entity + action`별 허용 Tool과 기대 결과를 적용
- [x] Document Agent가 인자 없는 검증 문서 Tool을 실제 `Runner`로 호출하고 내부 파이프라인은 결정적으로 유지
- [x] Document 입력은 Runtime이 발급 출처를 확인한 `verified_input_refs`만 사용하고 자연어 참조는 거부
- [x] 같은 부모 과제의 복수 `verified_input_refs`를 최대 5개까지 순서대로 변환·분석
- [x] Mission Service는 typed `action`, `mission_id`, `filter`로만 변경·조회 동작 결정
- [x] Manager의 `agent.as_tool()` 등록과 SDK handoff 경로 제거
- [x] 별도 결과 합성 Agent 호출 제거
- [x] 검증된 목록·상세·재생 결과는 Runtime이 원문 보존 형식으로 결정적 조합
- [x] 프롬프트에 누적된 ID·고유명사별 버그 예외를 타입·resolver·테스트로 이동

### MCP와 검증 참조

- [ ] 하위 호환 도구까지 모든 결과에 `FOUND`, `NOT_FOUND`, `AMBIGUOUS`, `AUTH_REQUIRED`,
  `PARSER_CHANGED`, `TEMPORARY_FAILURE`, `INVALID_REQUEST` typed 상태 적용
- [x] `list_course_announcements(course_query, term, limit)` 업무 단위 Tool 추가
- [x] `list_course_assignments(course_query, term, filters)` 업무 단위 Tool 추가
- [x] `list_course_lectures(course_query, week, only_unwatched)` 업무 단위 Tool 추가
- [x] `resolve_lecture(course_query, week, title_query)`가 실제 `lecture_id`를 확정하고 불투명 참조 발급
- [x] `play_resolved_lecture(verified_reference)`에서 모델의 ID 복사 제거
- [x] `preview_resolved_lecture(verified_reference)`에서 원시 `lecture_id` 미리보기 경로 차단
- [x] Dashboard 동기화용 `get_dashboard_snapshot()` 계약과 Startup·Heartbeat 연결
- [ ] SyncService와 별도 stdio MCP 프로세스의 브라우저 작업을 공유 Lock/Gateway로 직렬화
- [x] 고수준 강좌·강의 Tool에서 0개·1개·여러 후보를 NOT_FOUND·FOUND·AMBIGUOUS로 처리
- [x] 고수준 Tool 상태를 Agent 문장과 무관하게 Runtime 결과·오류 코드로 결정적으로 매핑

### 문맥·Trace·평가

- [x] 종류별 `VerifiedEntitySnapshot`을 기본 문맥으로 구현하고 기존 문자열은 호환용으로만 유지
- [x] 강좌·공지·과제·강의·첨부 후보를 typed reference로 보존
- [x] TUI 대화 기록, 자연어 요약과 검증 엔터티 문맥 분리
- [x] 사용자·시스템 요청 하나를 `conversation_id` group의 Agents SDK trace로 묶기
- [x] 모든 Agents SDK 실행에서 민감 모델·Tool payload를 trace에서 제외
- [x] 대표 다중 턴 후속 요청의 Tool 경계·ID·모호성 처리 회귀 테스트 추가
- [ ] 실제 E-Class smoke test에서 공지 상세, 특정 과제 상세, 복수 강의 선택과 재생 검증

### 완료 조건

```text
강좌 목록 → “2주차 영상” → “그거 재생”
과제 목록 → “첫 번째 자세히” → “PDF 요약”
공지 목록 → “1번 본문”
과목명 오타 → 실제 후보 확인
같은 주차 영상 여러 개 → 임의 선택하지 않고 되묻기
```

각 시나리오에서 모델이 ID를 생성·복사하지 않고 resolver의 검증 참조만 다음 단계에 전달해야 한다.
전문 결과의 고유명사·숫자·날짜·URL은 별도 LLM 합성 없이 그대로 표시돼야 한다.

현재 자동화 회귀 테스트는 통과하도록 유지한다. 다만 자동화 테스트 통과가 실제 E-Class 화면에서
위 다중 턴 시나리오를 모두 수행했다는 뜻은 아니므로 실제 smoke test는 별도 미완료로 유지한다.

---

## 9. TUI 마무리와 배포

### TUI

- [x] 상단 상태바·왼쪽 강의/이번 주 과제 패널·오른쪽 대화창·하단 명령바 분할 레이아웃
- [x] 현재 열린 강의의 완료 여부와 MCP 원본 진도율 상시 체크리스트
- [x] 상시 강의·출석 체크를 E-Class 로그인 직후 기본 선택 학기로 고정
- [x] MCP Activity 대신 기본 학기의 7일 이내 과제를 강좌명·마감과 함께 표시
- [x] 방학·강좌 0개 시 `수강 강의 없음`, 강좌는 있으나 열린 영상 0개 시 별도 안내
- [x] 초기 화면과 일반 대화 화면 디자인 통일
- [x] 작은 터미널에서 대화 기록 세로 스크롤 및 스크롤바 드래그 지원
- [x] 네이비·블루·스카이 기본색에서 파생한 다단계 시스템 팔레트 적용
- [x] 시스템 창 바깥 터미널 기본 배경 적용
- [x] 상황별 패널 크기와 형태 변화
- [x] 스트리밍 응답 표시
- [x] 현재 대화창을 유지하며 전문 Agent 실제 실행 시간만큼 표시되는 비차단 `작업 중...` 행
- [x] Manager 중간 계획창과 작업 결과창 전환 제거, 고정 대화창에서 최종 결과만 교체
- [x] 일시적인 Manager 계획·결과 통합 실패 1회 안전 재시도
- [x] 능동 알림과 사용자 요청을 시각적으로 구분
- [x] `SYNCING`, `PROACTIVE_ALERT`, `USER_TASK`, `PLAYBACK`, `AUTH_REQUIRED` 상태
- [x] 현재 Tool과 작업 진행률 표시
- [x] 영상 중지 단축키
- [x] 마지막·다음 동기화 시각 표시

### 테스트

- [x] Manager typed plan과 Runtime custom orchestration 배정 테스트
- [x] 복합 작업 순서 테스트
- [x] MCP 구조화 상태·검증 참조·만료 참조 자동화 계약 테스트
- [x] Snapshot 중복 변경 방지 테스트
- [x] 능동 알림 중복 방지 테스트
- [x] 세션 만료와 자동 재로그인 1회 제한 테스트
- [x] Textual 장시간 실행과 Timer 정리 테스트
- [x] Guardrail·승인·민감정보 테스트

### 배포

- [x] 앱·E-Class MCP·Document MCP를 포함하는 Dockerfile
- [x] Selkies 웹 화면·오디오와 Chromium을 포함하는 `Dockerfile.desktop`
- [x] Windows·macOS·Linux 공통 Desktop Compose 서비스와 로컬 HTTPS 포트 구성
- [x] 앱과 MySQL 8.0용 Docker Compose 구성 및 `docker compose config` 검증
- [x] Staging·Production MySQL 분리
- [x] Docker Compose Secret 및 배포 플랫폼 Secret 주입 경로 설정
- [x] Alembic migration과 DB 백업·복구 절차
- [x] Playwright, MCP, OpenAI, Ollama 오류 관측
- [x] 실제 E-Class 읽기 전용 smoke test

### 9단계 검증 기록 (2026-07-22)

- 전체 자동화 테스트 `210개` 통과
- Staging·Production Compose 병합 설정 검증 통과 및 서로 다른 MySQL DB·볼륨 확인
- `eclass-quest:stage9` Docker 이미지 실제 빌드 통과
- 컨테이너의 비권한 `eclass` 사용자와 앱·E-Class MCP·Document MCP import 확인
- 실제 E-Class 기본 학기 및 2026년 1학기 읽기 전용 smoke test 통과
- OpenAI 연결 smoke test는 LMS 데이터를 전달하지 않는 별도 프로세스로 분리해 통과

여기서 완료한 실제 E-Class smoke test는 로그인·강좌·공지·과제·첨부·강의 목록의 읽기 경로를
검증한 것이다. 8.5단계에 남겨 둔 다중 턴 Agent 시나리오와 실제 영상 재생 검증까지 완료했다는
의미는 아니다.

별도의 Gateway, Cron worker, 상시 Scheduler 컨테이너는 배포 범위에 포함하지 않는다.

---

## 최종 완료 기준

- TUI 실행 직후 E-Class 상태를 자동 동기화한다.
- TUI가 켜진 동안 주기적으로 최신 정보를 확인한다.
- 새 공지·과제·미시청 강의와 마감 임박 항목을 Manager가 먼저 알린다.
- Manager typed plan에 따라 Runtime이 필요한 전문 Agent와 실제 MCP·Tool을 실행한다.
- 과제 첨부문서를 MarkItDown과 Qwen으로 분석한다.
- Mission·Checklist·Deadline Service가 검증된 결과를 계산·저장하고 Manager가 중요성을 설명한다.
- TUI 종료 시 Timer, Worker와 브라우저 자원이 정상 종료된다.
- 아이디·비밀번호·쿠키와 민감정보가 DB, 로그, Agent context에 노출되지 않는다. 개발 자격증명은
  git에서 제외된 `.env`, 운영 자격증명은 Secret Manager에만 둔다.
- 제출·삭제와 자동 출석 취득을 수행하지 않는다.

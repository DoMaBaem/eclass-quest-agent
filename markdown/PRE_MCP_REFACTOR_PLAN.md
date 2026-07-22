# MCP 구현 전 프로그램 구조 리팩터링 기록

> **역사 문서:** 이 파일은 MCP를 구현하기 전에 `System Companion`과 `Triage`를 제거하고 Manager
> Runtime의 첫 기반을 만들었던 당시 계획과 체크 기록이다. 현재 MCP와 동기화 기능은 이미 구현돼
> 있으므로 이 문서의 “추가할 파일”, “아직 MCP가 없음”, 4-Agent와 `agent.as_tool()` 설명을 현재
> 목표로 해석하지 않는다. 현재 목표는 [`Architecture.md`](./Architecture.md), 진행 상태는
> [`ROADMAP.md`](./ROADMAP.md) 8.5단계를 따른다.

현재 목표 Agent는 Manager·E-Class·Document 총 3개다. Mission·Checklist·Heartbeat·resolver·
Guardrail·TUI는 결정적 Python 서비스이며, Runtime이 Manager의 `entity + action + slots` typed plan을
검증해 specialist를 명시적으로 호출한다. E-Class 작업은 operation별 허용 MCP Tool·기대 결과
정책을 적용한다. 별도 handoff, `agent.as_tool()` 등록과 결과 합성 Agent 호출은 사용하지 않는다.

## 1. 문서 목적

이 문서는 기존 `System Companion → Triage → 전문 Agent 대기` 구조를 당시의
`LMS Manager Agent → 전문 Agent Tool` 구조로 변경하기 위해 작성한 리팩터링 기록이다.
System Companion·Triage·Checklist·WorkflowRunner 제거와 `ProactiveAssistantRuntime` 전환은
완료됐다. 이후 실제 사용에서 책임 혼합 문제가 확인됐으므로 남은 작업 판단에는 이 문서의 옛
체크리스트가 아니라 최신 로드맵을 사용한다.

이번 범위에서는 실제 공지·과제·강의 조회, 영상 재생, 첨부파일 다운로드 같은 MCP Tool을 만들지
않는다. Agent와 Runtime이 실제 MCP를 안전하게 연결할 수 있는 상태까지만 준비한다.

---

## 2. 리팩터링 전 코드 상태 기록

```text
app/main.py
└─ EclassQuestApp
   └─ WorkflowRunner
      ├─ System Companion Agent
      │  ├─ CHAT → 직접 응답
      │  └─ TASK → Triage 호출
      └─ Triage Agent
         └─ 전문 Agent 이름만 결정
```

당시 전문 Agent는 실제 Tool이 없는 선언만 존재했고, `WorkflowRunner`는
`HANDOFF_PENDING` 결과를 반환했다. 현재는 `ProactiveAssistantRuntime`이 전문 Agent 경계를 실행하고
Tool 미연결 기능은 `CAPABILITY_NOT_READY`로 종료한다.

### 현재 유지할 기반

- Textual `EclassQuestApp`과 상태별 시스템 창
- OpenAI 응답 스트리밍 처리 방식
- Pydantic 데이터 검증 방식
- MySQL 비동기 연결과 Snapshot Repository
- Playwright 직접 로그인과 암호화 세션 저장·복원
- 연도·학기 기반 강좌 탐색 Adapter
- E-Class/Document Agent의 좁은 책임 원칙

### 현재 발견된 구조 문제

1. 모든 입력이 대화형 Agent를 먼저 지나 실제 행동보다 챗봇 응답이 전면에 나온다.
2. System Companion과 Triage가 각각 OpenAI API를 호출해 한 요청에 불필요한 모델 호출이 생긴다.
3. Triage는 Agent 이름만 결정하고 실제 전문 Agent를 실행하지 않는다.
4. `WorkflowState`가 CHAT/TASK와 Triage 결과에 강하게 묶여 있다.
5. TUI가 `WorkflowRunner`, companion delta, handoff 이벤트 이름에 직접 의존한다.
6. `OpenAiApiKeyRequiredError`가 Triage 파일에 있어 다른 Agent가 Triage에 역으로 의존한다.
7. 스트리밍 JSON 추출기가 System Companion 파일에 묶여 있어 재사용하기 어렵다.
8. `Checklist Agent`는 표현 변환만 담당해 능동적인 미션 관리 책임이 부족하다.
9. Runtime 이벤트에 `TRIAGE_COMPLETED`만 있고 시작·능동 알림·세션 만료 이벤트가 없다.
10. 현재 작업 트리에 `tests/` 폴더가 존재하지 않는다.
11. 최초 Alembic migration이 `Base.metadata.create_all()`을 호출해 향후 ORM 변경이 과거 migration
    결과까지 바꾸는 문제가 있다.

---

## 3. MCP 이전 완료 상태

MCP 구현을 시작하기 전 프로그램 구조는 다음 모습이어야 한다.

```text
app/main.py
└─ EclassQuestApp
   └─ ProactiveAssistantRuntime
      ├─ 사용자 요청 이벤트
      ├─ 시스템 이벤트 계약
      ├─ Input/Output Guardrail
      └─ LMS Manager Agent
         ├─ E-Class Agent 계약
         ├─ Document Analysis Agent 계약
         └─ Mission Agent 계약
```

아직 MCP가 없으므로 전문 Agent에 실제 LMS Tool을 연결하지 않는다. 실제 데이터를 얻지 못한
상태에서 성공 결과나 가짜 과제 목록을 반환하는 Mock 경로도 만들지 않는다.

MCP가 필요한 요청은 다음과 같은 명시적 상태로 종료한다.

```text
CAPABILITY_NOT_READY
```

이는 최종 서비스 기능이 아니라 MCP 연결 전 구조 검증을 위한 임시 오류 상태다. MCP 읽기 Tool이
연결되는 즉시 제거한다.

---

## 4. 파일별 변경 요약

### 4.1 제거 완료된 파일

아래 파일은 Manager 경로와 TUI 전환을 확인한 뒤 제거했다.

| 파일 | 제거 이유 | 제거 시점 |
|---|---|---|
| `app/agent/system_companion_agent.py` | CHAT/TASK 챗봇 관문을 제거 | Manager TUI 전환 후 |
| `app/agent/triage_agent.py` | Manager가 전문 Agent를 직접 선택 | Manager TUI 전환 후 |
| `app/agent/checklist_agent.py` | Mission Agent로 역할 확대 | Mission Agent 테스트 후 |
| `app/agent/workflow_runner.py` | Manager 중심 Runtime으로 교체 | Runtime 테스트 후 |

`__pycache__`와 `.pytest_cache`는 소스 구조가 아니므로 코드 리팩터링 대상으로 다루지 않는다.

### 4.2 추가할 파일

```text
app/
├─ agent/
│  ├─ manager_agent.py
│  ├─ mission_agent.py
│  ├─ errors.py
│  └─ streaming.py
│
├─ runtime/
│  ├─ __init__.py
│  ├─ assistant_runtime.py
│  ├─ event_queue.py
│  └─ events.py
│
├─ guardrails/
│  ├─ __init__.py
│  ├─ input_guardrail.py
│  ├─ output_guardrail.py
│  └─ policy.py
│
└─ schemas/
   ├─ manager.py
   └─ runtime.py

tests/
├─ test_manager_agent.py
├─ test_assistant_runtime.py
├─ test_runtime_schemas.py
├─ test_guardrails.py
├─ test_tui_runtime.py
└─ test_storage_models.py
```

`sync_service.py`, `deadline_service.py`, 실제 Tool Guardrail은 E-Class MCP 읽기 Tool과 함께 구현한다.
MCP가 없는데 빈 SyncService나 가짜 LMS Provider를 먼저 만들지 않는다.

### 4.3 수정할 파일

| 파일 | 수정 내용 |
|---|---|
| `app/config.py` | 동기화 간격과 기본 학기 설정 추가, Agent 공통 설정 유지 |
| `.env.example` | 새 환경변수 예시 추가 |
| `app/schemas/workflow.py` | Companion/Triage 타입 제거 후 공통 Agent 실행 타입만 유지 |
| `app/schemas/domain.py` | 구형 `MissionResult` import를 새 Manager/Mission 결과로 교체 |
| `app/agent/eclass_agent.py` | `PurposeCode.supports()` 제거, Tool 주입 가능한 factory로 변경 |
| `app/agent/document_agent.py` | 구조화 출력과 Tool 주입 가능한 factory로 변경 |
| `app/tui/app.py` | `WorkflowRunner` 대신 `ProactiveAssistantRuntime` 사용 |
| `app/tui/events.py` | Triage 이벤트를 Runtime·Agent·능동 알림 이벤트로 교체 |
| `app/storage/models.py` | Mission, 알림, Sync, Agent 실행 모델 반영 |
| `alembic/versions/*` | 재현 가능한 migration으로 정리하고 새 revision 추가 |
| `app/main.py` | 생성 책임은 유지하되 새 Runtime이 TUI 내부에서 생성되도록 import 확인 |
| `markdown/MAIN_EXECUTION_FLOW_GUIDE.md` | 코드 전환 완료 후 새 실행 흐름으로 별도 갱신 |

---

## 5. 새 데이터 계약

### 5.1 제거할 타입

`app/schemas/workflow.py`에서 다음 타입을 제거한다.

```text
CompanionMode
SystemCompanionDecision
TriageDecision
WorkflowState.decision
```

`PurposeCode`는 Triage 분류용 이름이므로 제거하고, 필요하면 감사 로그용 `CapabilityCode`로 바꾼다.

```text
ECLASS_READ
VIDEO_CONTROL
DOCUMENT_ANALYSIS
MISSION_MANAGEMENT
GENERAL_RESPONSE
```

### 5.2 Runtime 이벤트

`app/schemas/runtime.py`에 사용자 입력과 시스템 이벤트의 공통 계약을 둔다.

```python
class RuntimeEventType(str, Enum):
    USER_REQUEST = "USER_REQUEST"
    STARTUP_BRIEFING = "STARTUP_BRIEFING"
    LMS_CHANGED = "LMS_CHANGED"
    DEADLINE_WARNING = "DEADLINE_WARNING"
    ATTENDANCE_WARNING = "ATTENDANCE_WARNING"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    MANUAL_SYNC_REQUESTED = "MANUAL_SYNC_REQUESTED"


class RuntimeEvent(BaseModel):
    event_id: str
    event_type: RuntimeEventType
    user_id: str
    payload: dict[str, object]
    occurred_at: datetime
```

`payload`에는 구조화된 최소 데이터만 넣고 쿠키, storage state, 비밀번호, 원본 HTML을 넣지 않는다.

### 5.3 Manager 결과

`app/schemas/manager.py`에 Manager의 사용자용 최종 계약을 둔다.

```python
class ManagerStatus(str, Enum):
    COMPLETED = "COMPLETED"
    NO_ACTION = "NO_ACTION"
    CAPABILITY_NOT_READY = "CAPABILITY_NOT_READY"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FAILED = "FAILED"


class ManagerResult(BaseModel):
    status: ManagerStatus
    message: str
    should_notify: bool
    priority: str
    suggested_actions: list[str]
    error_code: ErrorCode | None = None
```

- 사용자 요청 결과는 일반적으로 `should_notify=true`다.
- 중요하지 않은 시스템 이벤트는 `NO_ACTION`, `should_notify=false`로 조용히 종료한다.
- MCP 연결 전 LMS 요청은 `CAPABILITY_NOT_READY`로 명확하게 반환한다.
- Manager가 실제 Tool 결과 없이 과제·마감·출석 사실을 생성하지 못하게 한다.

### 5.4 Assistant 문맥

기존 `ConversationContext`를 제거하고 사용자 요청과 시스템 이벤트를 모두 지원하는
`AssistantContext`로 교체한다.

```text
conversation_id
safe_summary
turn_count
last_request_id
last_event_id
last_verified_entity_refs
```

전체 대화 원문, 인증정보와 확인되지 않은 LMS 사실은 저장하지 않는다.

---

## 6. Agent 파일 변경

### 6.1 `manager_agent.py` 추가

Manager는 별도 대화형 Agent와 Triage를 합친 것이 아니라, 실행 책임을 가진 새로운 최상위 Agent다.

```text
책임
├─ 사용자 요청과 시스템 이벤트 이해
├─ Tool 없이 답할 수 있는 일반 대화 처리
├─ 필요한 전문 Agent 선택
├─ 복합 작업 순서 결정
├─ 전문 Agent 결과 통합
├─ 능동 알림 필요 여부 판단
└─ 검증된 사실만 최종 보고
```

MCP 이전에는 실제 LMS Tool이 없으므로 `build_manager_agent()`가 빈 전문 Agent를 억지로 호출하게
하지 않는다. 다음 두 구성을 분리한다.

```python
build_manager_agent(settings, specialist_tools=[])
build_manager_agent(settings, specialist_tools=[...])  # MCP 연결 이후
```

일반 대화는 처리할 수 있지만 LMS 행동 요청은 `CAPABILITY_NOT_READY`로 종료한다.

### 6.2 `eclass_agent.py` 수정

- `PurposeCode` import와 `supports()`를 제거한다.
- `settings`와 실제 MCP Tool 목록을 factory 인자로 받을 수 있게 한다.
- MCP 이전에는 Agent 계약만 생성하고 실행 경로에는 등록하지 않는다.
- 출력 계약은 `EclassCollectionResult` 또는 Tool별 구조화 결과만 허용한다.
- LMS 제출·수정·삭제 금지 규칙을 유지한다.

예정 형태:

```python
def build_eclass_agent(settings: Settings, tools: list[Tool]) -> Agent:
    ...
```

### 6.3 `document_agent.py` 수정

- `settings`와 MarkItDown/Qwen Tool을 외부에서 주입받게 한다.
- MCP 이전에는 Agent 계약만 준비하고 Manager Tool로 등록하지 않는다.
- 출력 타입을 `DocumentAnalysisResult`로 고정한다.
- LMS 탐색과 다운로드 권한은 계속 금지한다.

### 6.4 `mission_agent.py` 추가

기존 Checklist Agent의 단순 표현 책임을 다음 범위로 확대한다.

```text
검증된 LMS 결과 입력
→ 우선순위 계산
→ 미션 생성 또는 갱신
→ 중복 미션 방지
→ 오늘·이번 주 미션 반환
```

MCP 이전에는 DB Repository와 Pydantic 계약까지만 연결한다. 가짜 LMS 항목으로 미션을 생성하는
시연 코드는 만들지 않는다.

### 6.5 공통 오류와 스트리밍 이동

```text
OpenAiApiKeyRequiredError
triage_agent.py → app/agent/errors.py

JsonStringFieldDeltaExtractor
system_companion_agent.py → app/agent/streaming.py
```

Manager 스트리밍에서도 같은 추출기를 재사용한다. 구형 Agent를 제거해도 공통 기능이 함께 사라지지
않게 하기 위한 이동이다.

---

## 7. Runtime 변경

### 7.1 `ProactiveAssistantRuntime`

기존 `WorkflowRunner`를 다음 책임으로 대체한다.

```text
ProactiveAssistantRuntime
├─ 사용자 요청을 RuntimeEvent로 변환
├─ 시스템 이벤트 수신
├─ Input Guardrail 실행
├─ Manager Agent 실행
├─ 스트림·Agent 이벤트를 TUI에 전달
├─ Output Guardrail 실행
├─ AssistantContext 갱신
└─ Agent 실행 상태 저장
```

예정 인터페이스:

```python
class ProactiveAssistantRuntime:
    async def handle_user_request(...) -> ManagerResult:
        ...

    async def handle_system_event(...) -> ManagerResult:
        ...

    async def shutdown(self) -> None:
        ...
```

MCP 이전에는 `handle_system_event()`의 계약과 `NO_ACTION` 처리만 검증한다. 실제 Startup Sync와
Heartbeat Timer는 MCP 읽기 Tool이 준비된 뒤 연결한다.

### 7.2 `event_queue.py`

`asyncio.Queue[RuntimeEvent]` 또는 Textual Message 중 한 방식을 선택한다. 초기에는 프로세스가 하나라
FastAPI, WebSocket, Redis Queue를 추가하지 않는다.

필수 규칙:

- 같은 이벤트 ID를 중복 처리하지 않는다.
- 종료 시 소비 task를 취소하고 기다린다.
- 큐에 인증정보와 Agent 내부 추론을 넣지 않는다.
- TUI가 종료되면 미처리 이벤트를 외부 서비스로 넘기지 않고 함께 종료한다.

---

## 8. Guardrail 변경

### 8.1 MCP 이전에 구현

#### Input Guardrail

- 학번·비밀번호·쿠키·토큰처럼 보이는 인증정보가 입력되면 실행 문맥과 요약에 남기지 않는다.
- 사용자 입력과 시스템 이벤트 payload의 길이·타입을 검증한다.
- 시스템 이벤트를 사용자 지침으로 해석하지 않게 이벤트 경계를 고정한다.

#### Output Guardrail

- Tool 실행 증거가 없으면 공지·과제·출석·마감 성공 사실을 출력하지 못하게 한다.
- API 키, 세션 경로, 쿠키, 원본 HTML과 내부 예외 문자열을 제거한다.
- 최종 결과를 `ManagerResult`로 검증한다.

#### Policy

```text
AUTO_READ
USER_REQUEST_REQUIRED
HUMAN_APPROVAL_REQUIRED
DISABLED
```

Tool이 아직 없더라도 권한 Enum과 정책 매핑 계약은 먼저 정의한다.

### 8.2 MCP 이후 구현

- 각 MCP Tool 입력과 결과를 검사하는 Tool Guardrail
- URL allowlist와 다운로드 경로 containment
- 상태 변경 Tool의 `needs_approval=True`
- 승인 interruption 저장과 run 재개

---

## 9. TUI 수정

### 9.1 의존성 교체

```text
현재
EclassQuestApp.runner = WorkflowRunner(settings)

변경
EclassQuestApp.runtime = ProactiveAssistantRuntime(settings)
```

`on_input_submitted()`은 더 이상 CHAT/TASK나 `state.decision`을 검사하지 않는다.

```text
입력
→ runtime.handle_user_request()
→ Manager 스트림 표시
→ ManagerResult 상태에 따라 화면 결정
```

### 9.2 이벤트 이름 교체

제거:

```text
TRIAGE_COMPLETED
COMPANION_RESPONSE
```

추가:

```text
RUNTIME_STARTED
MANAGER_STARTED
AGENT_DELEGATED
PROACTIVE_ALERT
NO_ACTION
AUTH_REQUIRED
CAPABILITY_NOT_READY
ERROR
```

MCP 연결 이후 추가:

```text
SYNC_STARTED / SYNC_COMPLETED
TOOL_STARTED / TOOL_SUCCEEDED / TOOL_FAILED
PLAYBACK_STARTED / PLAYBACK_COMPLETED
```

### 9.3 화면 동작

- 첫 화면과 일반 응답 화면의 통일된 디자인을 유지한다.
- 시스템 창 바깥의 터미널 기본 배경을 유지한다.
- Manager가 전문 Agent를 호출할 때만 짧은 `분석 중...` 화면을 표시한다.
- `CAPABILITY_NOT_READY`는 시스템 오류가 아니라 아직 연결되지 않은 기능 안내로 표시한다.
- 능동 알림용 진입 메서드를 미리 만든다.

```python
async def show_proactive_result(self, result: ManagerResult) -> None:
    ...
```

MCP 이전에는 이 메서드의 렌더링만 테스트하고 가짜 LMS 이벤트를 앱 실행 경로에 넣지 않는다.

### 9.4 Timer 연결 시점

`set_interval()`과 시작 동기화는 MCP 읽기 Tool과 `SyncService`가 완성된 뒤 연결한다. MCP 이전부터
빈 작업을 30분마다 실행하도록 만들 필요가 없다.

---

## 10. 설정 수정

`app/config.py`와 `.env.example`에 다음 동기화 값을 추가한다.

```env
ECLASS_SYNC_ON_STARTUP=true
ECLASS_SYNC_INTERVAL_MINUTES=30
```

기본 조회 학기는 `.env`에 고정하지 않는다. 학기 미지정 요청은 E-Class 화면의 기본
선택값을 사용하고, 사용자가 연도·학기를 둘 다 명시한 요청만 필터를 변경한다.

Pydantic 제약:

```text
sync_interval_minutes: 5 이상 1440 이하
```

MCP 이전에는 설정 검증만 수행한다. `ECLASS_SYNC_ON_STARTUP`을 실제로 사용하는 것은 SyncService
연결 이후다.

개인 개발용 `ECLASS_USERNAME`, `ECLASS_PASSWORD`, `ECLASS_AUTO_LOGIN` 환경변수를 허용한다.
자격증명은 자동 로그인 모듈에서만 사용하고 DB·로그·Agent·MCP context에는 전달하지 않는다.
운영 배포에서는 `.env` 파일 대신 Secret Manager가 같은 설정을 주입한다.

---

## 11. DB와 Migration 수정

### 11.1 최초 migration 고정

현재 `20260717_0001_initial_schema.py`는 다음 코드를 사용한다.

```python
Base.metadata.create_all(bind=op.get_bind())
```

이 방식은 미래에 `Base`에 모델을 추가하면 과거 0001 migration이 생성하는 테이블도 함께 달라진다.
새 DB에서는 0001이 새 테이블까지 만들고, 다음 migration이 같은 테이블을 다시 만들 수 있다.

MCP 전에 다음 중 하나로 반드시 정리한다.

```text
권장
0001을 현재 최초 스키마의 명시적인 op.create_table/op.create_index 코드로 고정
→ 기존 개발 DB의 실제 스키마와 일치하는지 검사
→ 새 변경은 0002 revision으로만 추가
```

개발 DB를 삭제하거나 다시 만들기 전에는 사용자에게 별도 확인을 받아야 한다.

### 11.2 새 ORM 모델

```text
MissionModel
├─ user_id
├─ source_type / source_id
├─ title
├─ status
├─ priority
├─ due_at
└─ completed_at

NotificationHistoryModel
├─ user_id
├─ entity_type / entity_id
├─ notification_type
├─ notified_at
└─ acknowledged_at

SyncHistoryModel
├─ user_id
├─ trigger_type
├─ status
├─ started_at / finished_at
├─ change_count
└─ error_code
```

기존 `WorkflowRunModel`은 바로 삭제하지 않는다. Manager Runtime 실행 기록에도 쓸 수 있도록
`trigger_type`, `event_id`, `initiated_by` 필드를 추가한 뒤 클래스 이름과 테이블 이름을 바꿀지는
실제 데이터 보존 여부를 확인해 결정한다.

### 11.3 Repository

- `MissionRepository` 추가
- `NotificationRepository` 추가
- `SyncHistoryRepository` 추가
- Snapshot Repository의 baseline/unchanged/updated 동작 유지
- 첫 수집 baseline에서는 새 미션 알림을 만들지 않는 기존 원칙 유지

---

## 12. 테스트 구조 복구

현재 `tests/` 디렉터리가 없으므로 MCP 작업 전에 다시 만든다.

### API 호출 없이 가능한 단위 테스트

- [ ] RuntimeEvent에 인증정보 필드가 들어오면 거부 또는 정제
- [ ] ManagerResult의 `NO_ACTION`은 `should_notify=false`만 허용
- [ ] `CAPABILITY_NOT_READY`가 LMS 성공 결과로 표시되지 않음
- [ ] Input/Output Guardrail의 비밀값 제거
- [ ] AssistantContext가 안전한 요약만 유지
- [ ] Runtime의 사용자 이벤트와 시스템 이벤트 분리
- [ ] 동일 event_id 중복 처리 차단
- [ ] TUI가 Manager 결과를 스트리밍 표시
- [ ] TUI가 능동 알림을 사용자 입력 없이 표시
- [ ] TUI 종료 시 Runtime shutdown 호출
- [ ] Mission/Notification/Sync ORM 모델 metadata 검증
- [ ] Alembic 0001 → 0002 fresh migration 검증

### 실제 OpenAI API를 사용하는 통합 테스트

기본 테스트 실행에는 포함하지 않고 명시적인 환경 플래그가 있을 때만 실행한다.

- [ ] 일반 학교 대화는 전문 Agent를 호출하지 않음
- [ ] LMS 작업 요청은 MCP 연결 전 `CAPABILITY_NOT_READY` 반환
- [ ] Manager 출력이 `ManagerResult`로 검증됨

Mock LMS 결과를 사용해 과제나 공지 성공 경로를 시연하지 않는다.

---

## 13. 안전한 적용 순서

### 1단계: 기반 문제 정리

- [x] `tests/` 구조 복구
- [x] Alembic 0001을 명시적인 고정 migration으로 변경
- [x] 새 0002 migration 설계·적용
- [x] 기존 개발 DB와 빈 임시 DB에서 migration·ORM 스키마 비교

### 2단계: 공통 코드 분리

- [x] `OpenAiApiKeyRequiredError`를 `agent/errors.py`로 이동
- [x] JSON 스트림 추출기를 `agent/streaming.py`로 이동
- [x] 기존 Agent 경로 회귀 테스트가 새 위치에서도 통과

### 3단계: 새 계약 추가

- [x] `RuntimeEvent`, `RuntimeEventType` 추가
- [x] `ManagerResult`, `ManagerStatus` 추가
- [x] `AssistantContext` 추가
- [ ] Permission Policy 추가
- [x] Companion/Triage 타입을 사용하는 위치 목록 확정 및 제거

### 4단계: 새 Agent 추가

- [x] LMS Manager Agent 작성
- [x] E-Class Agent factory 수정
- [x] Document Agent factory 수정
- [x] Mission Agent 작성
- [x] 세 전문 Agent를 `agent.as_tool()`로 등록하되 실제 내부 Tool이 없으면 호출 비활성화

### 5단계: Runtime 추가

- [x] `ProactiveAssistantRuntime` 작성
- [x] 사용자 요청 처리 구현
- [x] 시스템 이벤트 처리 계약 구현
- [ ] Guardrail 실행 순서 구현
- [x] Runtime 종료 처리 구현

### 6단계: TUI 전환

- [x] TUI 의존성을 `ProactiveAssistantRuntime`으로 교체
- [x] companion delta 콜백을 Manager delta 콜백으로 변경
- [x] Triage·decision 기반 화면 분기 제거
- [x] ManagerResult 기반 화면 분기 적용
- [x] 능동 알림 표시 진입점 추가

### 7단계: 구형 코드 제거

- [x] System Companion Agent 제거
- [x] Triage Agent 제거
- [x] Checklist Agent 제거
- [x] WorkflowRunner 제거
- [x] 사용하지 않는 Companion/Triage Pydantic 타입 제거
- [x] 실행 코드 import에서 구형 실행 경로 제거

### 8단계: MCP 착수 승인점

- [x] 전체 단위 테스트 통과
- [x] TUI가 ProactiveAssistantRuntime으로 실행됨
- [x] 일반 대화가 Manager에서 처리됨
- [x] LMS 요청이 가짜 결과 없이 `CAPABILITY_NOT_READY`로 종료됨
- [x] 시스템 이벤트가 `NO_ACTION` 또는 안전한 알림으로 처리됨
- [x] DB migration이 기존 DB와 새 빈 DB에서 모두 재현됨
- [ ] 비밀값이 TUI, Agent context, DB와 로그에 남지 않음

이 승인점을 통과한 뒤 `mcp_server/server.py`와 실제 읽기 Tool 구현을 시작한다.

---

## 14. MCP 이전에는 하지 않을 작업

- E-Class 공지·과제·강의 HTML parser 구현
- FastMCP `@mcp.tool()` 등록
- Agent에 빈 Tool 또는 가짜 LMS Tool 연결
- 주기적인 Startup Sync와 30분 Heartbeat 실제 활성화
- 영상 재생·중지 구현
- MarkItDown과 Qwen 실제 Tool 연결
- Tool Guardrail과 승인 interruption 구현
- 별도 Gateway, FastAPI, WebSocket, Redis, Celery 추가
- TUI 종료 후에도 실행되는 백그라운드 서비스 추가

---

## 15. 최종 파일 변화

```text
현재                                MCP 직전
────────────────────────────────────────────────────────────
system_companion_agent.py       → 제거
triage_agent.py                 → 제거
checklist_agent.py              → mission_agent.py
workflow_runner.py              → runtime/assistant_runtime.py
CompanionMode                   → 제거
TriageDecision                  → 제거
ConversationContext             → AssistantContext
UiEventType.TRIAGE_COMPLETED    → Runtime·Manager 이벤트
OpenAiApiKeyRequiredError       → agent/errors.py
JsonStringFieldDeltaExtractor   → agent/streaming.py
tests/ 없음                     → Runtime·Guardrail·TUI·DB 테스트 복구
```

MCP 직전의 프로그램은 LMS 데이터를 실제로 가져오지는 못하지만, Manager가 전체 실행을 소유하고,
전문 Agent와 Tool을 안전하게 연결할 계약·Runtime·TUI·DB·Guardrail 기반을 갖춘 상태여야 한다.

---

## 16. 설계 근거

당시 설계에서는 OpenAI Agents SDK의 `agents as tools` 방식을 선택했고, 전문 Agent가 이후 대화를
직접 소유할 때만 handoff를 검토했다. 현재 프로젝트는 실행 순서와 검증 참조를 Python이 강제해야
한다는 요구가 명확해져 17절의 custom orchestration으로 교정한다.

- [OpenAI Agents SDK orchestration and handoffs](https://developers.openai.com/api/docs/guides/agents/orchestration)
- [OpenAI Agents SDK guardrails and approvals](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)

---

## 17. MCP 구현 후 확인된 구조 보정

MCP와 실제 다중 턴 대화를 연결한 뒤 다음 문제가 확인됐다.

```text
Manager가 계획 JSON 생성
→ Runtime이 전문 Handler를 수동 분배
→ 일부 Agent 정의는 agent.as_tool()로도 등록
→ E-Class만 실제 Agent + MCP 경로 사용
→ Document와 Mission은 Handler가 Tool·Repository 직접 실행
→ 별도 Manager 호출이 결과를 다시 합성
```

즉 하나의 실행 경로에 SDK Agent Tool 선언, custom dispatcher와 별도 합성 호출이 함께 있었다.
또한 하나의 검증 결과 요약 문자열과 화면 단위 MCP Tool을 모델이 연결하면서 다음 유형의 오류가
발생할 수 있었다.

- 과목 ID와 강의 ID 혼동
- “첫 번째”, “그거” 같은 후속 대상 유실
- 과목명·담당자·제목 변형
- 특정 과제를 요청했는데 전체 목록으로 범위 확대
- 실제 성공한 MCP 결과를 Agent가 실패로 다시 표현

### 확정한 최종 실행 계약

```text
사용자 또는 시스템 이벤트
→ Input Guardrail
→ Manager Agent가 entity + action + slots typed plan 생성
→ Runtime이 plan·순서·한도·권한과 Operation Policy 검증
→ E-Class Agent 또는 Document Agent 명시적 호출
→ MCP·Tool 결과와 verified reference 검증
→ Runtime이 원문 보존 형식으로 결과를 결정적 조합
→ Output Guardrail
→ TUI
```

Agent는 다음 3개뿐이다.

```text
Manager Agent
E-Class Agent
Document Agent
```

다음은 Agent가 아니다.

```text
MissionService
ChecklistService
DeadlineService
Heartbeat / SyncService
ID resolver
Guardrail / Approval
TUI / Trace / Repository
```

### 보정 작업

- [x] `Mission Agent` 정의·Manager Tool 등록 제거, enum은 Service 실행 대상으로 교체
- [x] 기존 Mission Handler·Repository 기능을 LLM 없는 `MissionServiceHandler`로 정리
- [x] Manager의 specialist `agent.as_tool()` 등록과 handoff 주입 지점 제거
- [x] Manager는 typed plan만 만들고 Runtime이 specialist를 명시적으로 호출
- [x] 작업 대상을 `entity + action + slots`로 고정하고 해당 종류의 검증 Snapshot만 연결
- [x] E-Class 작업별 허용 MCP Tool과 기대 결과를 제한
- [x] 별도 synthesis Agent 호출 제거
- [x] 검증된 전문 결과의 목록·본문·ID·URL은 Runtime이 결정적으로 조합
- [x] E-Class MCP에 과목명·주차 중심 업무 Tool 6개와 Dashboard Snapshot Tool 추가
- [x] 강의 재생·미리보기에 만료되는 불투명 검증 참조 사용
- [x] 고수준 Tool에 `FOUND`, `NOT_FOUND`, `AMBIGUOUS`를 포함한 typed 상태 적용
- [x] 고수준 Tool 상태를 Agent 문장과 무관하게 Runtime 결과로 결정적 매핑
- [x] 종류별 검증 Snapshot을 기본 문맥으로 추가하고 이전 문자열은 호환용으로만 유지
- [x] 요청 하나를 묶는 trace와 다중 턴 후속 요청 회귀 테스트 추가
- [x] Agents SDK trace에서 민감 모델·Tool payload 제외
- [ ] 하위 호환 저수준 Tool 전체를 고수준 상태 계약으로 통일
- [x] Dashboard 동기화 전용 `get_dashboard_snapshot()` 구현 및 SyncService 연결
- [ ] 실제 E-Class에서 위 다중 턴 시나리오 smoke test

자동화 회귀 테스트는 계속 통과하도록 유지한다. 라이브 smoke test 완료 여부는
[`ROADMAP.md`](./ROADMAP.md)의 8.5단계에서 갱신하며, 이 역사 문서 앞부분의 체크 표시를 새 목표가
완료됐다는 근거로 사용하지 않는다.

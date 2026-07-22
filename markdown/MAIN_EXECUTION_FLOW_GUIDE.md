# E-Class Quest `main()` 실행 흐름과 Python 문법 가이드

이 문서는 OS별 실행 파일이나 `python -m app.main`을 실행했을 때의 코드 흐름과, 반영된 3-Agent
구조에서 각 계층이 맡아야 할 일을 설명한다. 제거된 `System Companion → Triage → WorkflowRunner`
프로토타입은 더 이상 현재 실행 흐름이 아니다.

---

## 1. 한눈에 보는 실행 흐름

```text
일반 사용자:
docker compose --profile desktop up -d --build
→ Webtop/Selkies HTTPS 웹 데스크톱
→ scripts/desktop_start.sh
→ Alembic migration
→ python -m app.main

네이티브 개발자:
Windows: run.ps1 또는 run.cmd
macOS·Linux: run.sh
→ python -m scripts.local_launcher
→ python -m app.main
→ app.main.main()
→ get_settings()
→ EclassQuestApp(settings) 생성
→ Textual의 App.run() 실행
→ compose()로 화면 생성
→ on_mount()로 Timer·시작 동기화 등록
└─ 이후 이벤트 루프
   ├─ 사용자 Enter → on_input_submitted()
   │  → ProactiveAssistantRuntime.handle_user_request()
   │  → Manager → Operation Policy → E-Class/Document specialist
   │  → Runtime 결과 검증·결정적 조합 → TUI
   ├─ Heartbeat → SyncService.sync()
   │  → E-Class MCP → MySQL 비교 → 필요할 때 Manager 알림
   └─ 종료 → on_unmount()
      → Timer·Worker·MCP·브라우저·Runtime 정리
```

현재 Agent는 `Manager`, `E-Class`, `Document` 3개다. Heartbeat, Checklist, Deadline, Mission,
Guardrail, resolver와 TUI는 Agent가 아니라 Python 코드다.

---

## 2. 관련 파일 지도

| 파일 | 역할 |
|---|---|
| `Dockerfile.desktop` | TUI·Agent·MCP·Chromium과 HTTPS 웹 화면·오디오를 포함한 배포 이미지 |
| `scripts/desktop_start.sh` | Desktop 컨테이너에서 migration 후 TUI 자동 실행 |
| `run.sh` | macOS·Linux에서 공통 Python 런처 호출 |
| `run.ps1`, `run.cmd` | Windows에서 공통 Python 런처 호출 |
| `scripts/local_launcher.py` | Docker MySQL 시작·상태 확인·migration 후 TUI 실행 |
| `app/main.py` | 설정을 읽고 TUI를 시작하는 진입점 |
| `app/config.py` | `.env`를 검증된 `Settings` 객체로 변환 |
| `app/tui/app.py` | 화면 구성, 입력, Timer, Background Worker와 결과 표시 |
| `app/runtime/assistant_runtime.py` | 요청 수명주기, 실행 순서·한도·문맥·오류 통제 |
| `app/agent/manager_agent.py` | 사용자 의도·범위와 typed 실행 계획 구조화 |
| `app/agent/run_config.py` | Agents SDK trace의 민감 payload 제외 설정 |
| `app/agent/eclass_agent.py` | E-Class 업무 의미 해석 |
| `app/agent/eclass_mcp_handler.py` | E-Class Agent와 로컬 MCP 실행 경계 |
| `app/agent/document_agent.py` | 문서 분석 Agent 정의 |
| `app/agent/document_handler.py` | MarkItDown MCP와 Qwen 분석 실행 경계 |
| `app/sync/service.py` | 주기 수집, Snapshot 비교와 능동 이벤트 생성 |
| `app/sync/deadline.py` | 마감 임박 조건 계산 |
| `app/storage/` | MySQL 모델·Repository·실행 기록 |
| `app/guardrails.py` | 입력·URL·경로·출력 검사 |
| `mcp_server/server.py` | 직접 작성한 E-Class MCP Tool 공개 |
| `mcp_server/adapters/` | Playwright로 실제 E-Class 조작 |

Mission 로직은 LLM 없는 `MissionServiceHandler`라는 결정적 서비스로 구분한다. 호환을 위해 파일명이
`mission_handler.py`이고 이전 클래스 별칭이 남아 있어도 이를 네 번째 Agent로 해석하면 안 된다.

---

## 3. Docker Desktop 실행과 네이티브 개발 런처

일반 배포에서는 Docker Desktop 이미지를 사용한다. Webtop 로그인 시 XFCE 터미널이
`scripts/desktop_start.sh`를 실행하며 migration 후 TUI를 시작한다. 같은 Desktop에서 Playwright의
headed Chromium이 열리므로 영상과 소리는 `https://localhost:3001`로 전달된다.

다음 OS별 실행 파일은 호스트 Python을 사용하는 개발 경로다.

`run.sh`, `run.ps1`, `run.cmd`는 각 OS에서 `.venv`의 Python 위치만 찾는다. 이후에는 모두
`python -m scripts.local_launcher`를 호출하므로 다음 절차가 운영체제와 무관하게 동일하다.

1. 기본 MySQL URL이면 Docker Compose의 MySQL 컨테이너를 시작한다.
2. 컨테이너가 `healthy`가 될 때까지 기다린다.
3. 다른 `MYSQL_URL`을 지정한 사용자는 Docker MySQL 시작을 건너뛴다.
4. Alembic migration으로 DB 테이블을 현재 코드와 맞춘다.
5. 같은 가상환경 Python으로 `app.main`을 실행하고 `--setup` 같은 인자를 전달한다.

Playwright는 호스트 OS에서 실행되므로 Windows·macOS·Linux에서 브라우저 창과 오디오를 사용자에게
직접 전달할 수 있다. MySQL만 Docker에 넣어 DB 설치 차이를 없앤다.

`cd app && python main.py`를 사용하면 프로젝트 루트가 Python import 경로에서 빠져
`ModuleNotFoundError: No module named 'app'`가 날 수 있다. 프로젝트 루트에서 Windows는
`.\run.ps1` 또는 `run.cmd`, macOS·Linux는 `./run.sh`를 사용한다.

---

## 4. `app/main.py`

현재 시작 코드는 최초 설정 확인을 거쳐 TUI를 실행한다.

```python
def main(argv: list[str] | None = None) -> int:
    args = parser.parse_args(argv)
    store = LocalSetupStore()

    if args.setup or not store.is_complete():
        run_setup_wizard(store, force=args.setup)

    settings = get_settings(store)
    EclassQuestApp(settings).run()
    return 0
```

### 4.1 import

```python
from app.config import get_settings
from app.setup_store import LocalSetupStore
from app.setup_wizard import run_setup_wizard
```

환경설정 조합 함수, 암호화 로컬 저장소, 최초 실행 마법사를 각각 가져온다. `app` 패키지 기준
import이므로 모듈 방식으로 실행하는 것이 안전하다.

### 4.2 `main(argv) -> int`

```python
def main(argv: list[str] | None = None) -> int:
```

- `def`: 함수를 정의한다.
- `main`: 함수 이름이다.
- `argv`: 테스트나 CLI에서 `--setup` 인자를 전달한다.
- `-> int`: 정수 반환을 기대한다는 타입 힌트다.

### 4.3 최초 설정 후 TUI 실행

```text
1. LocalSetupStore에서 기존 설정 확인
2. 설정이 없거나 --setup이면 터미널 마법사 실행
3. get_settings(store)로 저장값과 로컬 연결 설정 결합
4. EclassQuestApp(settings) 생성
5. Textual의 run() 실행
```

`EclassQuestApp` 소스에 `run()` 메서드가 보이지 않는 이유는 Textual의 `App` 클래스를 상속받았기
때문이다.

```python
class EclassQuestApp(App[None]):
    ...
```

Python은 `EclassQuestApp`에 메서드가 없으면 부모 `App`에서 찾는다. Textual의 `App.run()`이 터미널
화면과 이벤트 루프를 시작한다.

### 4.4 직접 실행 여부 검사

```python
if __name__ == "__main__":
```

- 파일을 실행하면 `__name__`이 `"__main__"`이다.
- 다른 파일이 import하면 `__name__`은 `"app.main"`이다.
- 따라서 import만 했을 때 TUI가 갑자기 실행되지 않는다.

`SystemExit(main())`는 `main()`의 반환값을 운영체제 종료 코드로 전달한다. 정상 종료는 `0`이다.

---

## 5. 최초 실행 설정과 설정 읽기

`main()`은 저장된 최초 실행 설정이 없으면 TUI를 열기 전에 모델·API 키·E-Class 계정을 입력받는다.
모델은 `data/config/settings.json`, 비밀값은 `data/config/credentials.enc`에 암호화해 저장한다.
이후 `get_settings()`가 저장값과 `.env`의 로컬 서비스 연결값을 합쳐 `Settings`로 검증한다.

주요 값:

```text
최초 실행 저장값: OpenAI API 키 / 모델 / E-Class 계정
MYSQL_URL
OLLAMA_URL
ECLASS_BASE_URL
ECLASS_STORAGE_STATE_ENCRYPTED
ECLASS_SESSION_ENCRYPTION_KEY
ECLASS_SYNC_ON_STARTUP
ECLASS_SYNC_INTERVAL_MINUTES
```

설정 객체를 한 번 만들어 TUI, Runtime, SyncService와 MCP 실행 경계에 전달한다. Agent 입력에는
API 키, E-Class 아이디·비밀번호, 세션 원문을 넣지 않는다.
저장값은 `./run.sh --setup`으로 다시 입력할 수 있다.

---

## 6. `EclassQuestApp` 생성

`__init__()`은 아직 화면을 그리지 않고 앱 수명 동안 사용할 객체와 상태를 준비한다.

```text
settings
ProactiveAssistantRuntime
transcript
SyncService
Timer 참조
마지막·다음 동기화 시각
```

Runtime은 TUI 실행 중 한 번만 생성한다. 이렇게 해야 최근 대화, 검증 문맥, 실행 중 MCP와 취소
상태를 턴마다 잃지 않는다.

`SyncService`는 MySQL 설정이 있고 동기화를 켰을 때 생성한다. TUI 단위 테스트에서는 가짜
`sync_service`를 주입하거나 `enable_sync=False`로 화면만 검사할 수 있다. 이는 운영용 Mock LMS를
만드는 것과 다르다.

---

## 7. Textual 생명주기

### 7.1 `compose()`

`compose()`는 화면에 들어갈 위젯 트리를 선언한다.

```text
system-window
└─ inner-frame
   ├─ top-bar
   ├─ workspace
   │  ├─ sidebar
   │  │  ├─ ACTIVE LECTURES
   │  │  └─ THIS WEEK ASSIGNMENTS
   │  └─ main-panel
   │     ├─ 대화 RichLog
   │     ├─ 입력 Input
   │     └─ 동기화 시각
   └─ command-bar
```

`yield`는 결과를 한 번에 반환하고 끝내는 `return`과 다르게 위젯을 하나씩 Textual에 넘긴다.

```python
with Horizontal(id="top-bar"):
    yield Static("E-CLASS QUEST SYSTEM")
```

`with` 블록 안에서 생성한 위젯은 해당 컨테이너의 자식이 된다.

### 7.2 `on_mount()`

화면 위젯이 실제로 준비된 직후 한 번 호출된다.

```text
시계 Timer 등록
빈 Checklist 렌더링
첫 SYSTEM 메시지 표시
Heartbeat Timer 등록
설정이 켜져 있으면 Startup Sync Worker 시작
```

Timer 콜백은 네트워크 작업을 직접 기다리지 않는다. Background Worker를 시작해 UI 입력과 시계가
멈추지 않게 한다.

### 7.3 `on_unmount()`

TUI 종료 시 다음 순서로 정리한다.

```text
Timer 중지
Sync Worker 취소·완료 대기
SyncService 종료
Runtime 종료
MCP·Playwright 자원 정리
```

`finally` 또는 종료 훅에서 정리하지 않으면 브라우저와 자식 MCP 프로세스가 남을 수 있다.

---

## 8. 사용자 입력 흐름

Textual은 Input에서 Enter가 눌리면 다음 메서드를 호출한다.

```python
async def on_input_submitted(self, event: Input.Submitted) -> None:
```

`async def`는 함수 안에서 `await`로 네트워크·Agent 작업을 기다릴 수 있다는 뜻이다. 기다리는 동안
다른 TUI 이벤트까지 전부 막는 일반 동기 함수와 다르다.

실행 순서:

```text
1. 빈 입력 제거
2. 수동 동기화 명령인지 확인
3. 입력창 잠금
4. USER 메시지를 transcript에 추가
5. 같은 대화창에 “작업 중...” 행 추가
6. runtime.handle_user_request() 대기
7. 작업 애니메이션 종료
8. 입력창 잠금 해제
9. 같은 행을 최종 결과 또는 실패 메시지로 교체
```

```python
try:
    result = await self.runtime.handle_user_request(message)
finally:
    event.input.disabled = False
```

`finally`는 성공하거나 예외가 발생해도 실행된다. API나 MCP 오류 뒤에 입력창이 영원히 잠기는 것을
막는다.

---

## 9. Runtime의 책임

`ProactiveAssistantRuntime`은 Agent 그 자체가 아니다. 한 요청이 안전하게 끝나도록 통제하는
Python 실행 관리자다.

```text
handle_user_request(message)
→ request_id 생성
→ Input Guardrail
→ RuntimeEvent(USER_REQUEST)
→ Manager 의도·작업 계획
→ 검증 문맥으로 생략된 대상 보완
→ entity·action·slots 계약과 Operation Policy 검사
→ 필요한 specialist 실행
→ 결과 구조·근거 검사
→ Runtime이 검증 결과를 결정적으로 조합
→ Output Guardrail
→ 안전한 대화 문맥 갱신
→ Audit 기록
```

현재 구조는 별도 Synthesis Agent나 두 번째 Manager LLM 호출을 두지 않는다. Manager가 사용자
문맥과 작업 범위를 소유하고, Runtime은 검증된 specialist 결과를 원문 보존 형식으로 조합한다.

Runtime이 직접 강제할 사항:

- 최대 작업 단계
- 허용되지 않은 순서와 중복 단계 차단
- 영상 재생의 명시적 사용자 요청 확인
- `entity + action`별 허용 Tool과 기대 결과
- `FOUND`, `NOT_FOUND`, `AMBIGUOUS`, `AUTH_REQUIRED` 상태 처리
- 검증된 엔터티 참조와 오류 코드 보존
- 취소와 자원 정리

---

## 10. Manager 실행

Manager는 사용자 자연어와 안전한 문맥을 받아 구조화된 의도를 만든다.

```text
mode: CHAT | TASK
tasks[]:
  agent: E-Class Agent | Document Analysis Agent | Mission Service
  capability: ECLASS_QUERY | VIDEO_PLAY | DOCUMENT_ANALYSIS | MISSION_MANAGEMENT
  entity: COURSE | ANNOUNCEMENT | ASSIGNMENT | ATTACHMENT | LECTURE | GRADE | DOCUMENT | MISSION
  action: LIST | DETAIL | DOWNLOAD | PLAY | STOP | PREVIEW | ANALYZE | ...
  slots:
    year / semester
    course_query / query
    week / ordinal / filter / mission_id
  instruction: 사람이 읽는 보충 설명
  verified_input_refs: []  # Runtime 전용; Manager는 항상 비움
reply
```

Manager가 해야 하는 것은 “무슨 일을 원하는가”를 판단하는 일이다. LMS ID를 찾아 복사하거나
Tool 호출 순서를 자유 형식 문장으로 작성하는 일은 Operation Policy와 resolver가 맡는다.

Manager가 출력하는 `verified_*_target`은 신뢰하지 않는다. Runtime이 이를 먼저 비우고, 작업의
`entity + action`과 일치하는 직전 typed Snapshot에서 단일 대상 또는 같은 부모 과제의 첨부 묶음을
검증할 수 있을 때만 채운다.
`verified_input_refs`도 같은 원칙으로 비운 뒤, 앞 단계나 현재 세션에서 실제 검증된 다운로드 참조만
Document 단계에 최대 5개 주입한다. Document Handler는 `instruction` 안의 참조 문자열을 실행 권한으로
파싱하지 않으며, 복수 파일은 원래 첨부 목록 순서대로 하나씩 변환·분석한다.

CHAT이면 전문 Agent와 MCP를 부르지 않고 Manager가 직접 답한다. TASK이면 Runtime이 typed intent에
맞는 Operation Policy를 실행한다.

---

## 11. E-Class specialist 실행

E-Class 경계는 다음 책임으로 제한한다.

```text
typed LMS intent
→ Operation Policy가 허용 Tool과 기대 결과 제한
→ E-Class Agent가 허용된 고수준 MCP Tool 선택
→ MCP가 resolver와 Playwright 실행
→ typed Tool envelope 반환
→ handler가 성공 Tool과 evidence reference 검증
→ Runtime에 SpecialistResult 반환
```

E-Class MCP는 별도 stdio 자식 프로세스로 실행된다.

```text
현재 Python 실행 파일
→ python -m mcp_server.server
→ FastMCP Tool 목록 제공
→ Playwright Adapter 사용
```

조회 task에는 영상 제어 Tool을 노출하지 않고, 명시적인 영상 task일 때만 재생·중지 Tool을 허용한다.

기존 구현처럼 모델이 `list_courses → course_id 복사 → list_lectures → lecture_id 복사`를 직접
조립하면 ID가 섞일 수 있다. 현재는 다음처럼 resolver가 검증 참조를 전달한다.

```text
resolve_lecture(course_query, week, title_query)
→ verified lecture reference
├─ play_resolved_lecture(reference)
└─ preview_resolved_lecture(reference)
```

---

## 12. Document specialist 실행

문서 분석은 E-Class가 발급한 검증된 `download_id`가 있을 때만 시작한다.

```text
Document Agent
→ MarkItDown MCP
→ MarkdownConversionResult 검증
→ QwenDocumentAnalyzer
→ DocumentAnalysisResult 검증
→ Runtime의 검증 결과 조합
```

MarkItDown은 파일 형식을 Markdown으로 바꾸고, Qwen은 그 Markdown을 요약·구조화한다. 둘 중 하나가
실패하면 Document Agent는 내용을 추측하지 않는다. 복수 요청은 파일명별 결과를 구분하며 일부 파일만
변환할 수 없으면 성공 파일 결과를 보존하고 실패 파일에는 원인을 표시한다.

---

## 13. Mission·Checklist·Deadline 흐름

이 셋은 Agent 호출 경로가 아니다.

```text
SyncService가 검증된 과제·강의 상태 수집
→ ChecklistService가 화면 항목 계산
→ DeadlineService가 남은 시간과 완료 상태 계산
→ MissionService가 중복 없이 로컬 미션 저장·조회·완료
→ 중요한 구조화 이벤트만 Manager에 전달
```

Manager는 “내일 마감이므로 먼저 처리하는 편이 좋다”처럼 설명할 수 있지만, 마감 시간 계산이나
미션 DB 갱신을 창작하지 않는다.

---

## 14. Startup·Heartbeat 동기화

`on_mount()`와 Timer는 `_start_sync_worker()`를 호출한다.

```text
_perform_sync(trigger)
→ SyncService.sync(trigger)
→ get_dashboard_snapshot() 서비스 계약으로 기본 학기 5종 일괄 읽기
→ MySQL Snapshot 비교
→ 강의·과제 Checklist 갱신
→ 마지막·다음 확인 시각 갱신
→ 생성된 중요 RuntimeEvent만 Manager에 전달
```

상태별 처리:

- `COMPLETED`: 왼쪽 패널과 시각 갱신, 중요 이벤트 표시
- `AUTH_REQUIRED`: 로그인 또는 자동 갱신 안내
- `FAILED`: 기존 사실을 성공처럼 표시하지 않고 연결 확인 안내

첫 수집은 비교 기준인 baseline으로 저장한다. 변화가 없으면 Manager API를 호출하지 않는다.
Dashboard Snapshot은 강좌·공지·과제·강의·성적 메타데이터만 포함하며 파일을 자동 다운로드하지 않는다.

---

## 15. 대화 기록과 Agent 문맥은 다르다

TUI의 `transcript`는 이번 실행에서 사용자가 보는 기록이다. Agent에게 매번 최대 500개 전체를
보내는 것은 아니다.

현재 문맥은 두 종류로 분리한다.

```text
ConversationContext
├─ 최근 안전 대화
└─ 누적 요약

VerifiedEntityContext
├─ 직전 강좌 후보
├─ 직전 공지 후보
├─ 직전 과제 후보
├─ 직전 강의 후보
├─ 직전 첨부 후보
└─ 종류별 마지막 선택
```

“그거”, “1번”, “첫 번째”의 의미는 Manager가 해석한다. 하지만 최종 ID는 Python 문맥 저장소가
검증된 후보에서 꺼낸다. 비밀번호·쿠키·HTML 원문은 어떤 문맥에도 넣지 않는다.

---

## 16. 콜백과 스트리밍 문법

Runtime은 진행 상황을 TUI에 알리기 위해 콜백을 받을 수 있다.

```python
async def show_runtime_progress(event_name, agent_name):
    ...

result = await runtime.handle_user_request(
    message,
    on_progress=show_runtime_progress,
)
```

함수를 호출한 결과가 아니라 함수 자체를 인자로 넘긴다. Runtime은 실제 단계가 바뀔 때 콜백을
호출한다.

Agent의 JSON 조각을 그대로 화면에 스트리밍하면 중간 계획이나 깨진 구조가 보일 수 있다. 현재 TUI는
실행 시간 동안 같은 `SYSTEM` 행에 애니메이션을 표시하고, 검증이 끝난 최종 결과로 교체한다.
향후 자연어 최종 응답을 stream할 때도 구조화 필드 파싱과 Output Guardrail을 통과한 내용만 보인다.

---

## 17. 오류가 사용자에게 전달되는 방식

내부 예외 전체를 TUI에 출력하지 않는다.

```text
내부 예외
→ Runtime 또는 Handler에서 typed 상태로 변환
→ Audit에는 component·state·error_code 기록
→ TUI에는 사용자가 취할 수 있는 설명만 표시
```

대표 상태:

```text
NOT_FOUND
AMBIGUOUS
AUTH_REQUIRED
PARSER_CHANGED
TEMPORARY_FAILURE
POLICY_BLOCKED
```

`MANAGER_FAILED` 같은 내부 코드를 화면 제목으로 크게 표시하기보다 “잠시 후 다시 시도해 주세요”,
“두 강의 중 하나를 선택해 주세요”처럼 실제 원인과 다음 행동을 대화창에 보여 준다.

---

## 18. 흐름을 확인할 때 볼 지점

문제가 생기면 다음 순서로 확인한다.

```text
1. TUI가 USER 입력을 Runtime에 전달했는가
2. Manager의 entity·action·slots가 요청 범위를 보존했는가
3. 올바른 Operation Policy와 허용 Tool이 선택됐는가
4. resolver가 실제 후보와 ID를 반환했는가
5. MCP Tool이 성공 typed result를 반환했는가
6. handler가 검증 근거를 보존했는가
7. Runtime이 조합한 최종 표시 결과가 원문을 보존했는가
8. Output Guardrail이 필요한 정보를 과도하게 제거하지 않았는가
```

한 요청 전체를 같은 `request_id` trace로 묶으면 스크린샷만 보고 추측하지 않고 실패 단계를 바로
찾을 수 있다.

Manager, E-Class, Document의 모든 Agents SDK 실행은 `trace_include_sensitive_data=False`를 사용한다.
따라서 강좌명·과제 본문·문서 내용·Tool 인자 원문은 SDK trace payload에 포함하지 않고, 로컬
Audit에는 단계·상태·오류 코드만 남긴다.

---

## 19. 핵심 정리

1. `main()`은 설정을 읽고 Textual의 상속받은 `run()`을 호출한다.
2. Textual이 `compose`, `on_mount`, 입력 이벤트와 `on_unmount`를 호출한다.
3. TUI는 LMS를 직접 조작하지 않고 Runtime에 요청을 전달한다.
4. Manager는 의도·문맥·최종 답변을 소유한다.
5. E-Class와 Document만 전문 Agent다.
6. Runtime은 Agent가 아니라 순서·권한·상태를 강제하는 Python 코드다.
7. Heartbeat, Checklist, Mission, resolver와 Guardrail도 Python 서비스다.
8. 실제 LMS 작업은 직접 작성한 E-Class MCP와 Playwright가 수행한다.
9. 검증 엔터티 문맥이 자연어 대화 요약과 분리돼야 후속 요청의 ID가 흔들리지 않는다.
10. 최종 구조에서는 별도 Synthesis Agent 호출 없이 Runtime이 검증 결과를 결정적으로 조합한다.

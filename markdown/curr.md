# E-Class Quest 현재 구성

## 1. 서비스 목표

한성대학교 E-Class의 강좌·공지·과제·강의 정보를 실제로 조회하고, TUI가 켜져 있는 동안
새 항목과 마감 임박 일정을 먼저 알려 주는 로컬 실행형 LMS Agent AI다.

## 2. 주요 기술과 실행 환경

| 구분 | 사용 기술 | 역할 |
|---|---|---|
| Agent AI | OpenAI Agents SDK + 최초 설정에서 선택한 모델 | 요청 해석, 구조화 계획, 응답 생성 |
| Local AI Tool | Ollama + `qwen3:0.6b` | 변환된 첨부문서 구조화 분석 |
| LMS 자동화 | 직접 작성한 E-Class MCP + Playwright Chromium | 실제 E-Class 조회·다운로드·영상 제어 |
| 문서 변환 | MarkItDown MCP | PDF·DOCX·PPTX 등 지원 문서를 Markdown으로 변환 |
| Database | MySQL 8.0 + SQLAlchemy + Alembic | 스냅샷·변경·미션·실행 이력 저장 |
| UI | Textual TUI + 웹 데스크톱 | 대화, 강의·과제 패널, 능동 알림, 영상 창 제공 |
| 배포 | Docker Compose | 앱·MySQL·Ollama·Chromium을 같은 환경으로 실행 |

기본 배포는 사용자 PC에서 실행되는 로컬 서비스다. Docker Desktop만 설치하면 소스를 clone한 뒤
`docker compose --profile desktop up -d --build`로 실행하며, 최초 실행 시 모델·API 키·E-Class
계정을 입력한다. 비밀번호와 API 키는 화면에 표시하지 않고 암호화된 로컬 설정으로 보관한다.

## 3. Agent 구조

```text
LMS Manager Agent
├─ Runtime → E-Class Agent → E-Class MCP → Playwright → E-Class
├─ Runtime → Document Analysis Agent → MarkItDown MCP → Ollama Qwen
└─ Runtime → Mission Service → MySQL
```

- **LMS Manager Agent**: 대화 문맥을 유지하고 요청을 `entity + action + slots` 계획으로 만든다.
- **E-Class Agent**: 검증된 MCP 결과만 사용해 강좌·공지·과제·강의·성적 작업을 수행한다.
- **Document Analysis Agent**: 검증된 다운로드 파일만 MarkItDown과 Qwen으로 분석한다.
- Agent 간 자유 handoff는 사용하지 않는다. Runtime이 허용 작업·순서·횟수·결과 형식을 통제한다.
- Heartbeat, Checklist, Deadline, Mission, Guardrail은 Agent가 아닌 결정론적 Python 서비스다.

## 4. 직접 작성한 E-Class MCP

E-Class MCP는 FastMCP stdio 서버이며 Playwright로 한성대학교 E-Class 한국어 화면을 조작한다.
HTML 전체를 Agent에 넘기지 않고 Pydantic으로 검증한 구조화 결과만 반환한다.

- 로그인 세션 확인, 만료 시 저장 계정으로 재로그인하고 암호화 세션 갱신
- 기본 학기 또는 지정 학기의 강좌 목록과 Dashboard Snapshot 조회
- 전체·특정 강좌의 공지/과제 목록 및 상세 내용 조회
- 과제 첨부파일 목록 확인과 최대 용량을 적용한 안전한 다운로드
- 주차별 강의 목록·진도·출석 상태 조회
- 검증된 강의 참조를 통한 영상 미리보기·재생·상태 확인·중지
- 성적 조회와 과목명 오타·후속 번호 요청을 위한 대상 확인

`get_dashboard_snapshot()`은 다운로드 없이 현재 강좌·과제·강의 상태를 한 번에 가져온다.
Startup과 30분 Heartbeat가 이를 MySQL의 이전 Snapshot과 비교해 새 공지·과제, 마감 임박,
미수강 강의를 찾고 TUI에 능동 알림과 왼쪽 체크리스트로 표시한다. 사용자의 “지금 업데이트해줘”
요청은 Heartbeat 시간을 기다리지 않고 즉시 새 Snapshot을 조회한다.

## 5. Document MCP와 파일 처리

E-Class MCP가 과제·공지의 실제 첨부 링크를 검증해 `download_id`를 발급하고 파일을 내려받는다.
Document Agent는 이 참조로 MarkItDown MCP를 호출해 Markdown으로 변환한 뒤, 로컬 Qwen Tool로
요약·요구사항·마감·주의사항을 구조화한다. 다운로드는 `data/downloads`에 저장되고 기본 24시간 뒤
정리되며, 임의의 로컬 경로·미검증 URL·용량 초과 파일은 거부한다.

## 6. 안전성과 현재 범위

- 계정·비밀번호·쿠키·API 키를 Agent 입력, MySQL, TUI, trace에 기록하지 않는다.
- URL·파일 경로·Tool 종류·결과 schema를 검증하며 Agent가 LMS ID나 사실을 추측하지 못하게 한다.
- 현재 어댑터는 한성대학교 E-Class DOM 기준이며 다른 학교는 별도 Adapter 구현이 필요하다.
- TUI가 꺼져 있으면 Heartbeat와 알림도 중지되며 별도 상시 백그라운드 서비스는 제공하지 않는다.
- E-Class 화면 구조나 영상 Player가 변경되면 Playwright 선택자 보수가 필요할 수 있다.

세부 설계는 [Architecture.md](./Architecture.md), 진행 현황은 [ROADMAP.md](./ROADMAP.md),
설치·실행 방법은 프로젝트 루트의 [README.md](../README.md)를 따른다.

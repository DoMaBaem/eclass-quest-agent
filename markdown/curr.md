# E-Class Quest 현황

## 목표

한성대학교 E-Class를 조회하고, TUI 실행 중 중요한 일정과 변경을 먼저 알려 주는 능동형 LMS 비서다.

| 구분 | 기술 | 책임 |
|---|---|---|
| Agent AI | OpenAI Agents SDK + 최초 설정에서 선택한 모델 | 요청 이해, typed 계획, CHAT 응답 |
| Document Tool | MarkItDown MCP + Ollama `qwen3:0.6b` | 첨부문서 변환·구조화 분석 |
| Database | MySQL 8.0 | LMS 스냅샷·변경·미션·실행 기록 |
| Browser | Playwright Chromium | 실제 E-Class 조회와 사용자 요청 영상 제어 |
| UI | Textual TUI | 대화, 체크리스트, 능동 알림 |

## 목표 실행 구조

```text
Manager Agent                         사용자 문맥·작업 범위·응답 정책 소유
├─ Runtime 호출 → E-Class Agent      검증된 E-Class 도메인 작업
└─ Runtime 호출 → Document Agent     내려받은 첨부문서 분석

결정적 Python 서비스
├─ Runtime·Operation Policy·Heartbeat·Sync
├─ Checklist·Deadline·Mission 생성
├─ Guardrail·Approval·ID resolver
└─ MySQL·TUI·Trace
```

Agent는 총 3개다. `Mission`, `Checklist`, `Heartbeat`, `Guardrail`은 Agent가 아니라 동일 입력에
항상 같은 결과를 내는 Python 서비스다. Manager는 E-Class의 사실이나 ID를 추측하지 않고,
전문 작업은 Runtime이 검증 결과를 원문 보존 형식으로 조합한다.

## 현재 구현과 남은 검증

- [x] 직접 작성한 Playwright 기반 E-Class MCP와 읽기·재생·다운로드 Tool
- [x] TUI 실행 중 Startup·Heartbeat 동기화와 MySQL Snapshot 비교
- [x] Manager 계획, E-Class MCP 실행, MarkItDown·Qwen 분석 경로
- [x] 입력·출력·URL·파일 경로 기본 Guardrail
- [x] Agent를 Manager·E-Class·Document 3개로 제한하고 Mission을 LLM 없는 Service로 교체
- [x] Manager `entity + action + slots` plan → Runtime 명시적 호출, 별도 합성 LLM 제거
- [x] 작업별 허용 Tool·기대 결과 정책과 고수준 MCP Tool 7개 적용
- [x] 기본 학기 Dashboard Snapshot을 Startup·Heartbeat와 연결
- [x] 재생·미리보기용 불투명 강의 참조와 만료·변조 거부
- [x] 과목·공지·과제·강의·첨부별 typed 검증 Snapshot 저장
- [x] 과제 상세과 첨부 Snapshot 연결, 같은 과제 복수 첨부 최대 5개 다운로드·분석
- [x] 요청 전체 trace, 민감 trace payload 제외, 다중 턴 자동화 회귀 테스트 적용
- [x] TUI 실행 상태·능동 알림·Tool 진행률 표시와 검증된 재생 세션 직접 중지
- [x] Staging·Production Compose, Secret 주입, migration, 백업·복구, health/smoke 구성
- [x] 전체 테스트 210개, 실제 Docker 이미지 빌드, 실제 E-Class 읽기 smoke 검증
- [x] 최초 실행 모델·API 키·E-Class 계정 설정과 암호화 로컬 저장
- [ ] 실제 E-Class에서 공지 상세·특정 과제·복수 강의 선택·재생 smoke test

자세한 목표 구조는 [`Architecture.md`](./Architecture.md), 적용 순서는
[`ROADMAP.md`](./ROADMAP.md)를 따른다.

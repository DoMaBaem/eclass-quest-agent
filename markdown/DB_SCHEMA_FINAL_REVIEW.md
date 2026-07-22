# MySQL 스키마 최종 검토

## 결론

현재 스키마는 E-Class 조회, 변경 감지, 능동 알림, 미션 관리, 영상 제어 감사 로그와 문서 임시
다운로드를 구현하기에 적절하다. 개발 DB와 새 빈 DB 모두 Alembic `20260720_0004`까지 적용했고
ORM 자동 비교 결과 추가 변경 사항은 0건이다.

## 테이블 역할

| 영역 | 테이블 | 핵심 역할 |
|---|---|---|
| 사용자·인증 | `users`, `eclass_sessions` | 사용자 설정, 암호화 세션 파일 참조와 검증 상태 |
| LMS 최신 상태 | `courses`, `assignments`, `lectures`, `announcements`, `grades`, `attachments` | 사용자별 정규화된 최신 데이터 |
| 변경 감지 | `entity_snapshots`, `change_events` | fingerprint 이력과 실제 변경 이벤트 |
| 능동 관리 | `missions`, `notification_history`, `sync_history` | 미션, 중복 알림 방지, 동기화 결과 |
| 실행 감사 | `workflow_runs`, `playback_runs` | Agent 요청과 영상 제어 실행 상태 |
| 문서·문맥 | `downloaded_files`, `conversation_summaries` | 만료 파일과 안전한 대화 요약 |

## 이번에 확정한 핵심 사항

- Snapshot과 ChangeEvent 고유키에 `entity_type`을 포함해 과제·강의 ID 충돌을 방지했다.
- 과제·강의·공지·성적을 `(user_id, course_eclass_id)`로 강좌에 연결했다.
- `attachments`를 추가하고 임시 다운로드가 실제 첨부 메타데이터를 참조하게 했다.
- 영상 진도와 출석 판정을 분리하도록 `attendance_status`, `completed_at`을 추가했다.
- 알림은 고정 1회 제한 대신 `dedupe_key`로 마감·상태 버전별 재알림이 가능하다.
- ChangeEvent는 Manager 전달 대기·처리 상태와 고유 Runtime 이벤트 ID를 보존한다.
- 마감, 시청 종료, 공지 시간, 학기 조회에 맞는 복합 인덱스를 추가했다.
- 세션 상태와 오류 코드만 DB에 저장하며 비밀번호·쿠키 원문은 저장하지 않는다.
- DB CHECK 제약으로 학기, 연도, 진도율, 파일 크기의 비정상 값을 차단한다.

## 구현 시 지킬 규칙

- MySQL `DATETIME`에는 시간대 정보가 보존되지 않으므로 모든 시각을 UTC로 변환해 저장한다.
- 강좌를 먼저 upsert한 뒤 과제·강의·공지·성적을 저장해야 외래키를 만족한다.
- 학교 전체 공지는 `course_eclass_id=NULL`로 저장할 수 있다.
- 다형 첨부 부모(`parent_type`, `parent_eclass_id`)는 DB 외래키 대신 수집 계층에서 검증한다.
- `dedupe_key`에는 엔터티, 알림 단계, 기준 마감 또는 상태 버전을 포함한 안정적인 해시를 쓴다.
- 최신 상태 테이블에서 사라진 항목을 정리해도 이력은 Snapshot과 ChangeEvent에 남긴다.

## 아직 데이터로 검증할 항목

실제 E-Class 화면에서 과제 제출 시각과 강의 출석 상태가 항상 제공되는지는 MCP 구현 단계에서
확인해야 한다. 제공되지 않는 값은 추측하지 않고 `NULL` 또는 `UNKNOWN`으로 저장한다.

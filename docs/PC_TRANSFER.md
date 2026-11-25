# 관리 서버 개발 PC 이전

AI 저장소의 [이전 가이드](https://github.com/david95j2/care-document-ai/blob/codex/initial-source/docs/PC_TRANSFER.md)와 `tools/transfer.py`, `tools/restart.ps1`을 사용한다.

- 소스는 Git clone, DB·원본 문서·민감 로그는 별도 ZIP으로 전달한다.
- 실제 DB 경로를 export 설정의 `manager_db`에 지정한다.
- online backup은 실행 중 WAL까지 포함한 일관된 SQLite 스냅샷을 만든다.
- DB에 저장된 계정, 검토 상태, 문서와 원본 매출 행을 함께 보존한다.
- snapshot 이후 신규 입력은 포함되지 않으므로 실제 전환 직전 직원 입력을 멈추고 최종 백업한다.
- 복원 대상은 반드시 새 private 폴더다. 기존 DB에 덮어쓰지 않는다.
- 기존 `.runtime.lock`, WAL/SHM을 복사하지 않는다. 서비스 시작 시 기존 migration 검증 절차를 사용한다.
- 재시작 시 `-Service manager -ManagerCode <이 저장소 경로> -Start`를 지정한다.
- 계정은 DB에서 승계한다. 비밀번호 초기화나 새 admin 생성을 자동으로 수행하지 않는다.
- ZIP은 자체 암호화되지 않는다. 접근 제한/암호화된 별도 저장장치로 전달한다.
- 현재 개발 서버는 127.0.0.1:8877이다. 내부망 배포·HTTPS·운영 서비스는 별도 단계다.
- 부팅 작업은 이번 이전 도구에서 등록하지 않는다.

복원 후 로그인→기존 문서 수/검토 값/직원 권한/매출 원본 행을 확인한다.
백업 시점과 이후 입력 여부도 확인한 뒤 사용자 전환을 결정한다.

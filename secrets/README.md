# 배포 Secret 파일 계약

실제 값은 Git과 Docker 이미지에 넣지 않는다. `scripts/init_deployment_secrets.py`가
`secrets/staging` 또는 `secrets/production`에 다음 파일을 권한 `0600`으로 만든다.

- `mysql_app_password.txt`
- `mysql_root_password.txt`
- `openai_api_key.txt`
- `eclass_username.txt`
- `eclass_password.txt`
- `eclass_session_key.txt`

Compose는 이를 `/run/secrets/<이름>`에 읽기 전용으로 연결한다. 배포 플랫폼의 Secret
Manager를 사용할 때도 같은 대상 파일 이름으로 마운트하면 애플리케이션 변경이 필요 없다.
`staging/`, `production/` 디렉터리는 `.gitignore` 대상이다.

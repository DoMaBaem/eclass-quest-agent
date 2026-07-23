FROM python:3.12-slim

WORKDIR /app

# Playwright 브라우저를 특정 사용자 홈이 아닌 이미지 공용 경로에 설치한다. 실행 단계에서
# 비-root 사용자로 전환해도 Chromium 실행 파일을 그대로 사용할 수 있다.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" eclass \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home eclass

COPY --chown=eclass:eclass app ./app
COPY --chown=eclass:eclass mcp_server ./mcp_server
COPY --chown=eclass:eclass document_mcp_server ./document_mcp_server
COPY --chown=eclass:eclass alembic ./alembic
COPY --chown=eclass:eclass alembic.ini ./alembic.ini
COPY --chown=eclass:eclass scripts ./scripts
COPY --chown=eclass:eclass run.sh ./run.sh

# Windows checkout에서 CRLF로 바뀐 셸 스크립트도 Linux 컨테이너에서 실행할 수 있게 한다.
RUN mkdir -p /app/data/audit /app/data/downloads /app/data/sessions \
    && chown -R eclass:eclass /app/data \
    && sed -i 's/\r$//' /app/run.sh /app/scripts/*.sh \
    && chmod +x /app/run.sh /app/scripts/*.sh

USER eclass

# Compose Secret은 파일로 마운트된다. 진입점이 필요한 값만 프로세스 환경으로 옮기고
# 비밀번호가 포함된 MYSQL_URL도 로그를 남기지 않은 채 메모리에서 조립한다.
ENTRYPOINT ["python", "/app/scripts/container_entrypoint.py"]

CMD ["python", "-m", "app.main"]

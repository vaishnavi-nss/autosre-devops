# Build from the repository root (the playbooks live outside this directory):
#   docker build -t autosre/remediation-dispatcher:dev -f remediation-dispatcher/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    ANSIBLE_COLLECTIONS_PATH=/app/collections \
    ANSIBLE_LOCAL_TEMP=/tmp/.ansible/tmp \
    ANSIBLE_REMOTE_TEMP=/tmp/.ansible/tmp

WORKDIR /app

COPY remediation-dispatcher/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt ansible-core==2.21.6

# The Kubernetes modules the playbooks use, installed at build time so a clean clone builds without a git-ignored folder.
COPY remediation-dispatcher/collections-requirements.yml .
RUN ansible-galaxy collection install -r collections-requirements.yml -p /app/collections

COPY remediation-dispatcher/app.py ./app.py
COPY ansible/ ./ansible/

RUN useradd --uid 10001 --no-create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8001

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]

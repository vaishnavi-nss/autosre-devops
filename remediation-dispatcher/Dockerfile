FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANSIBLE_COLLECTIONS_PATH=/app/collections

WORKDIR /app

COPY remediation-dispatcher/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt ansible-core==2.21.6

COPY remediation-dispatcher/app.py ./app.py
COPY remediation-dispatcher/collections/ ./collections/
COPY ansible/ ./ansible/

EXPOSE 8001

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]

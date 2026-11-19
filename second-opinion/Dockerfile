FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py opinion.py prometheus.py scoring.py ./
# Trained by train.py; see docs/evaluation.md for what it was trained on.
COPY models/second_opinion.joblib models/second_opinion.joblib
# Trained on our own injected faults (train_own.py, docs/evaluation.md run 3). Shadow mode only.
COPY models/own.joblib models/own.joblib
# Retrained on the older trials plus real-fault trials (eval_real.py). Opt-in only: load it with MODEL_PATH=models/own_real.joblib (launcher: SCORER_MODEL=own_real.joblib).
COPY models/own_real.joblib models/own_real.joblib
# Retrained on the older trials plus the Kubernetes-state fault trials (train_own.py), keeping only the fault kinds that held-out testing found reliably detectable.
COPY models/own_v2.joblib models/own_v2.joblib
ENV MODEL_PATH=models/own_v2.joblib

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

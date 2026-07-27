# Minimal MLflow tracking-server image for local development. Deliberately does not include
# torch/scikit-learn — the server only needs to store/serve run metadata and artifacts; training
# code (apps/trainer, Phase 10+) talks to it over HTTP as a client.
FROM python:3.12-slim

RUN pip install --no-cache-dir "mlflow>=3.14,<4"

EXPOSE 5000

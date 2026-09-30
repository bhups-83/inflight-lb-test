# Self-contained dummy WS backend image. Public base, no private deps.
# Build + push to YOUR registry, then point k8s/deployment.yaml at it
# (scripts/setup.sh does both when you pass IMAGE=...).
FROM python:3.12-slim
RUN pip install --no-cache-dir websockets==12.0
COPY dummy_ws_server.py /app/dummy_ws_server.py
WORKDIR /app
EXPOSE 8080
CMD ["python3", "dummy_ws_server.py"]

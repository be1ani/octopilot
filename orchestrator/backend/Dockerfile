FROM python:3.12-slim

WORKDIR /repo

# Orchestrator backend starts/stops agent containers via the Docker daemon.
# It needs the docker CLI in-container, plus the host docker socket mounted at runtime.
RUN apt-get update \
  && apt-get install -y --no-install-recommends docker.io ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY orchestrator/backend/requirements.txt /repo/orchestrator/backend/requirements.txt
RUN pip install --no-cache-dir -r /repo/orchestrator/backend/requirements.txt

COPY orchestrator/backend/app.py /repo/orchestrator/backend/app.py

EXPOSE 5050

ENV ORCH_PORT=5050
ENV ORCH_DOCKER_BIN=/usr/bin/docker
CMD ["python", "orchestrator/backend/app.py"]

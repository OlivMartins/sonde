FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY sonde_agent ./sonde_agent
RUN pip install . && useradd --uid 10001 --no-create-home agent

USER 10001
ENTRYPOINT ["python", "-m", "sonde_agent"]

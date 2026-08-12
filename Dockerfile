# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency layer first: source changes far more often than pyproject.toml,
# so this keeps rebuilds cheap during the collection milestones.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY alembic ./alembic

# Never run as root. The container has no business writing to its own image,
# and a compromised collector should not be able to.
RUN useradd --create-home --uid 10001 arbbot \
    && chown -R arbbot:arbbot /app
USER arbbot

ENTRYPOINT ["arbbot"]
CMD ["doctor"]

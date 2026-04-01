ARG PYTHON_BASE_IMAGE=docker.1panel.live/library/python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

ARG APT_MIRROR_HOST=mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
ENV PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}

RUN sed -i "s@deb.debian.org@${APT_MIRROR_HOST}@g; s@security.debian.org@${APT_MIRROR_HOST}@g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY alembic.ini pyproject.toml README.md /app/
COPY apps /app/apps
COPY infra /app/infra
COPY packages /app/packages

RUN python -m pip install \
    --no-cache-dir \
    --default-timeout=120 \
    --index-url "${PIP_INDEX_URL}" \
    --trusted-host "${PIP_TRUSTED_HOST}" \
    -e ".[dev]"

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

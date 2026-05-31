FROM python:3.11-slim

# git is needed for the wiki's own version history (git is the audit log/undo).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first for layer caching. pyproject.toml is the source of truth;
# the explicit pin here keeps the image build self-contained.
RUN pip install --no-cache-dir "fastmcp>=2.0,<3"

COPY src/ ./src/
COPY seed/ ./seed/
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

ENV WIKI_ROOT=/srv/wiki
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8765

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "-m", "wiki_server.server"]

#!/usr/bin/env bash
# Bring the database up to date, prove the schema matches the models, then serve.
#
# Migrations used to run after the container started, which meant a phone hitting
# the API in that window got a 500 on a missing column. Nothing serves traffic
# until the schema is verified now.
set -euo pipefail

python -c "from app.db import wait_for_db; wait_for_db()"
python -c "from app.db import init_db; init_db()"
python scripts/migrate.py
python -c "from app.db import verify_schema; verify_schema()"

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --proxy-headers \
    --forwarded-allow-ips '*'

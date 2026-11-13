#!/bin/sh
# Bind to $PORT (set by Render and similar PaaS); fall back to 8000.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"

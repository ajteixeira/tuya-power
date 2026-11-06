#!/usr/bin/env bash
cd "$(dirname "$0")"
exec /mnt/storage1/conda/envs/tuya-power/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080

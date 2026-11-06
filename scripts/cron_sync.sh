#!/usr/bin/env bash
# Sincronização horária para o cron. O flock impede execuções sobrepostas.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data
exec flock -n data/sync.lock bash -c '
  echo "=== $(date "+%F %T") ==="
  /mnt/storage1/conda/envs/tuya-power/bin/python scripts/sync_once.py
' >> data/sync.log 2>&1

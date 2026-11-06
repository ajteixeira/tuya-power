# Conda (micromamba), tal como no resto do projeto: sem pip nem venv
FROM mambaorg/micromamba:2.0.5

USER root
COPY environment.yml /tmp/environment.yml
RUN micromamba create -y -p /opt/env -f /tmp/environment.yml && micromamba clean -a -y

COPY app /app/app
USER mambauser

ENV PATH=/opt/env/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    DB_PATH=/app/data/tuya.db

WORKDIR /app
EXPOSE 8080

# Vivo = responde HTTP (401 por falta de credenciais também conta)
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import urllib.request as u, urllib.error as e, sys\ntry:\n u.urlopen('http://127.0.0.1:8080/', timeout=4)\nexcept e.HTTPError:\n pass\nexcept Exception:\n sys.exit(1)"]

ENTRYPOINT []
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]

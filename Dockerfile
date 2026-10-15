FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VOSK_MODEL_PATH=/app/models/vosk-model-small-ru-0.22 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1
WORKDIR /app

RUN --mount=type=secret,id=ca_bundle,mode=0444 \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && if [ -f /run/secrets/ca_bundle ]; then \
         printf 'Acquire::https::CaInfo "/run/secrets/ca_bundle";\n' > /etc/apt/apt.conf.d/99-build-ca; \
       fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99-build-ca
COPY requirements.txt requirements-vosk.txt ./
RUN --mount=type=secret,id=ca_bundle \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir -r requirements.txt -r requirements-vosk.txt
COPY vosk_model.py vosk_model_files.json download_vosk_model.py ./
ARG VOSK_MODEL_URL=https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip
RUN --mount=type=secret,id=ca_bundle \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    python download_vosk_model.py --url "$VOSK_MODEL_URL"
COPY app.py vosk_api.py ./
RUN chmod -R a+rX /app

USER 10001:10001
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]

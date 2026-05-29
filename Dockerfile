# Railway: собираем веб-сервис clipopus_web из корня репозитория.
# (Корневой Dockerfile нужен, т.к. Railway по умолчанию ищет Dockerfile в корне.)
FROM python:3.12-slim

# ffmpeg + ffprobe для ресайзера
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY clipopus_web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY clipopus_web/ .

ENV PORT=8000
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["sh", "-c", "echo \"[boot] starting uvicorn on port ${PORT:-8000}\" && python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]

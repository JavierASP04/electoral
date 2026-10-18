FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8005

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates
COPY static ./static

USER appuser

EXPOSE 8005

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8005} --worker-class gthread --workers ${WEB_CONCURRENCY:-2} --threads ${WEB_THREADS:-4} --timeout 60 --graceful-timeout 20 --max-requests 300 --max-requests-jitter 40 --access-logfile - --error-logfile - app:app"]

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

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8005} --workers 2 --timeout 120 --access-logfile - --error-logfile - app:app"]

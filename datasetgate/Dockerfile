FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir . gunicorn

# Copy application
COPY . .

# Collect static files
RUN python manage.py collectstatic --noinput 2>/dev/null || true

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn -c gunicorn.conf.py datasetgate.wsgi:application"]

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

# Run migrations and start server
EXPOSE 8080
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn datasetgate.wsgi:application --bind 0.0.0.0:8080 --workers 2 --timeout 120"]

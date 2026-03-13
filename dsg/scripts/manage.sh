#!/bin/bash
# Run a manage.py command locally or inside the Docker container,
# depending on whether the container is running.
if docker compose ps --status running 2>/dev/null | grep -q dsg; then
    docker compose exec dsg python manage.py "$@"
else
    python manage.py "$@"
fi

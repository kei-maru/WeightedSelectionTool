FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8765 \
    OPEN_BROWSER=0 \
    ALLOW_SHUTDOWN=0 \
    DB_PATH=/app/data/vrc_raffle.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py web_app.py fastapi_routes.py api.py core.py ./
COPY static ./static

RUN mkdir -p /app/data

EXPOSE 8765

CMD ["python", "main.py"]

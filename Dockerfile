# Timeweb Cloud Apps compatible
FROM python:3.11-slim

WORKDIR /app/src

COPY requirements.txt ./
COPY app/requirements.txt ./app/
RUN pip install -r requirements.txt && pip install -r ./app/requirements.txt

COPY . ./
RUN find /app/src -name "*.pyc" -delete && \
    find /app/src -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

ENV PORT=8080
EXPOSE $PORT

CMD ["python", "-m", "app.main"]

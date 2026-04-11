FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV BOT_TOKEN=8768806445:AAF9_pdz5Gx23wmZH87GEdlZ8pXP4hzgQB4
ENV TIMEZONE=Europe/Moscow

CMD ["python", "-m", "app.main"]
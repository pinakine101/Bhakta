# Timeweb Cloud Apps compatible
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libsdl2-mixer-2.0-0 \
    libsdl2-image-2.0-0 \
    libsdl2-ttf-2.0-0 \
    libsdl2-2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/src

COPY requirements.txt ./
COPY app/requirements.txt ./app/
RUN pip install -r requirements.txt && pip install -r ./app/requirements.txt

COPY . ./

ENV PORT=8080
EXPOSE $PORT

CMD ["python", "-m", "app.main"]

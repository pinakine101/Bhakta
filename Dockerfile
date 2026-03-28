# Базовый образ Python 3.11 (стабильный, есть все бинарные пакеты)
FROM python:3.11-slim

# Устанавливаем системные зависимости для pygame (SDL)
RUN apt-get update && apt-get install -y \
    libsdl2-mixer-2.0-0 \
    libsdl2-image-2.0-0 \
    libsdl2-ttf-2.0-0 \
    libsdl2-2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файл с зависимостями
COPY requirements.txt .

# Устанавливаем зависимости Python
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Указываем порт (Render ожидает, что приложение слушает порт $PORT)
ENV PORT=8080
EXPOSE $PORT

# Запуск бота (замените на вашу точку входа)
CMD ["python", "-m", "app.main"]
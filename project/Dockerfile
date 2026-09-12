# Офіційний образ Playwright — вже містить Chromium і всі системні залежності
# для headless-браузера на Linux. Версія образу має збігатися з версією
# пакета playwright у requirements.txt (інакше браузер і Python-клієнт розійдуться).
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]

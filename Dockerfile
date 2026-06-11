FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium de Playwright (ya incluido en la imagen base)
RUN playwright install chromium

COPY . .

CMD ["python", "main.py"]

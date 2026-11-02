FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
# openpyxl je potřeba jen pro přepočet pomůcky z Excelu (automatická / ruční aktualizace)
RUN pip install --no-cache-dir -r requirements.txt
# neviditelný prohlížeč pro čtení sdílených odkazů Mapy.com (spouští se jen na pár sekund při sdílení)
RUN pip install --no-cache-dir playwright==1.63.0 && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
COPY . .
# databáze vlaků a stažená pomůcka se ukládají do /app/var (doporučeno připojit Railway Volume)
ENV TRAINS_DB=/app/var/trains.db
EXPOSE 8000
CMD ["python", "-m", "app.server"]

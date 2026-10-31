FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
# openpyxl je potřeba jen pro přepočet pomůcky z Excelu (automatická / ruční aktualizace)
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# databáze vlaků a stažená pomůcka se ukládají do /app/var (doporučeno připojit Railway Volume)
ENV TRAINS_DB=/app/var/trains.db
EXPOSE 8000
CMD ["python", "-m", "app.server"]

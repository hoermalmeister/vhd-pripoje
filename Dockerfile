FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY . .
# databáze vlaků se vytvoří při startu ze staženého GTFS (viz app/server.py)
ENV TRAINS_DB=/app/var/trains.db
EXPOSE 8000
CMD ["python", "-m", "app.server"]

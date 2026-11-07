# Build context is the repo root (see docker-compose.yml) so the forecaster can be included
FROM python:3.12-slim
WORKDIR /app
COPY arduino-ingest/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY occupancy_forecaster/occupancy_core.py occupancy_forecaster/rolling_24h.py occupancy_forecaster/
COPY occupancy_forecaster/data/synthetic_occupancy.csv seed/
COPY arduino-ingest/app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--root-path", "/ingest", "--proxy-headers", "--forwarded-allow-ips", "*"]

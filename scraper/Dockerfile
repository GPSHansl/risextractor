FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py .
COPY defaults.yml /config/defaults.yml

CMD ["python", "scraper.py"]
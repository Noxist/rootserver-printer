FROM python:3.11-slim

# Systemabhängigkeiten für Druck und Fonts
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    fonts-dejavu-core fonts-noto-color-emoji libjpeg-dev libusb-1.0-0 libudev1 libffi-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Non-root user setup
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --create-home --shell /usr/sbin/nologin appuser

# Copy application code
COPY . .

USER 1000:1000

# FastAPI starten
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

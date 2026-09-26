FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    gdal-bin \
    libgdal-dev \
    g++ \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port $PORT"]
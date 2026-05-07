FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    libsndfile1 \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

COPY ./app .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100", "--log-config", "log_config.json"]

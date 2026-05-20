FROM python:3.11-slim
WORKDIR /app

# 系統依賴：PyMuPDF wheel 通常不需要額外 libs，但留 ca-certificates 供 HTTPS
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]

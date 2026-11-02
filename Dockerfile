FROM python:3.11-slim
# compilers are only needed if a wheel is missing for your CPU (Paradex's signing libraries)
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libgmp-dev libssl-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY static ./static
RUN useradd -m trader && mkdir /data && chown trader /data
USER trader
ENV DATA_DIR=/data BIND_HOST=0.0.0.0 PORT=8000 PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz',timeout=4)"
CMD ["python", "serve.py"]

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# libglib/libgl → OpenCV; fonts-dejavu → tipografía por defecto del renderer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-ocr.txt ./

# OCR opcional: docker compose build --build-arg INSTALL_OCR=true backend
ARG INSTALL_OCR=false
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && if [ "$INSTALL_OCR" = "true" ]; then pip install -r requirements-ocr.txt; fi

COPY app ./app
COPY tests ./tests
COPY pytest.ini ./pytest.ini

ENV DATA_DIR=/app/data
RUN mkdir -p /app/data/projects

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

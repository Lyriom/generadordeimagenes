FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# libglib/libgl → OpenCV; fonts-dejavu → tipografía por defecto del renderer;
# libgomp → onnxruntime y torch.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-ocr.txt requirements-sam.txt ./

RUN pip install --upgrade pip && pip install -r requirements.txt

# OCR (RapidOCR sobre onnxruntime). Se puede dejar fuera con
#   docker compose build --build-arg INSTALL_OCR=false backend
#
# La limpieza de OpenCV va en esta misma capa a propósito. rapidocr depende de
# `opencv_python`, el de escritorio, y al instalarse se queda con el módulo
# cv2: en un servidor sin pantalla son 90 MB de más y, peor, una versión que el
# resto del código no ha probado (llegó a poner la 5.0 sobre la 4.x).
# Deshacerlo en una capa aparte no ahorra nada: la capa anterior ya se los
# llevó.
ARG INSTALL_OCR=true
RUN if [ "$INSTALL_OCR" = "true" ]; then \
        pip install -r requirements-ocr.txt \
        && pip uninstall -y opencv-python opencv-contrib-python \
        && pip install --force-reinstall --no-deps "opencv-python-headless>=4.10,<5.0"; \
    fi

# SAM 2.1 para recortar productos. Se puede dejar fuera con
#   docker compose build --build-arg INSTALL_SAM=false backend
#
# torch va desde el índice de CPU: la rueda normal de PyPI arrastra CUDA y las
# librerías de nvidia, que son 3 GB inútiles en un servidor sin GPU.
# --no-build-isolation es obligatorio: sam2 importa torch para construirse y,
# sin esto, pip se monta un entorno aparte y vuelve a bajarse torch entero.
# git se instala y se borra en la misma capa porque sam2 no está en PyPI.
ARG INSTALL_SAM=true
ENV SAM_CHECKPOINT=/models/sam2.1_hiera_small.pt \
    SAM_VARIANT=sam2
RUN if [ "$INSTALL_SAM" = "true" ]; then \
        pip install --index-url https://download.pytorch.org/whl/cpu \
            "torch~=2.14.0" "torchvision~=0.29.0" \
        && apt-get update \
        && apt-get install -y --no-install-recommends git \
        && pip install --no-build-isolation -r requirements-sam.txt \
        && apt-get purge -y git && apt-get autoremove -y \
        && rm -rf /var/lib/apt/lists/* \
        && mkdir -p /models \
        && curl -fsSL -o "$SAM_CHECKPOINT" \
            https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt; \
    fi

# Red de seguridad: si alguna dependencia futura vuelve a colar el OpenCV de
# escritorio, la construcción falla aquí y no en producción con un cv2 que
# nadie ha probado. No ocupa nada: solo comprueba.
RUN python -c "\
import cv2, importlib.metadata as md;\
assert cv2.__version__.startswith('4.'), cv2.__version__;\
md.distribution('opencv-python-headless');\
print('cv2', cv2.__version__, '(headless)')"

COPY app ./app
COPY tests ./tests
COPY pytest.ini ./pytest.ini

ENV DATA_DIR=/app/data
RUN mkdir -p /app/data/projects

# El reconocedor latino se descargaría la primera vez que alguien analice un
# arte. Traerlo aquí quita esa espera y, sobre todo, la dependencia de que el
# servidor tenga salida a internet cuando alguien lo usa.
#
# Va después de COPY app y no junto al pip a propósito: así usa el proveedor de
# verdad en vez de repetir aquí qué modelo pedir, que es justo lo que se
# desincroniza. Cuesta unos segundos por construcción; se gana no tener dos
# sitios donde elegir el modelo.
#
# No corta la construcción si falla: un corte de red no justifica quedarse sin
# backend. Que quedó bien lo comprueba el flujo de despliegue, que sí falla.
RUN python -c "\
from app.providers.rapid_ocr import precargar_modelos;\
ok, detalle = precargar_modelos();\
print(('OCR: ' if ok else 'AVISO, OCR sin precargar: ') + detalle)"

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

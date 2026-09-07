# vision-service: everything with a heavy native build lives here, so the
# banking backend's image doesn't have to carry any of it.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# tesseract-ocr-ron: Romanian language pack - ID cards and bank statements
# are printed in Romanian.
#
# cmake/build-essential: face_recognition's dlib dependency has no prebuilt
# wheel for every platform, so pip compiles it from source here. This is the
# slow part of the build (~4 minutes) and the reason it is confined to this
# image: the backend rebuilds in seconds without it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-ron \
    cmake build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir ".[dev]" && rm -rf build *.egg-info

COPY tests ./tests

EXPOSE 8100

# No --reload: this service's code changes far less often than the backend's,
# and the reloader would keep a second copy of dlib resident.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--log-level", "info"]

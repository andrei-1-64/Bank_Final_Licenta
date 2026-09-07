FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# No apt packages at all any more. tesseract (OCR) and cmake/build-essential
# (dlib, for face embeddings) moved to vision/Dockerfile along with the code
# that needed them - see vision/app/main.py. That is what took this image
# from ~1.7GB and a ~4 minute build down to a plain-Python one.

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir ".[dev]" && rm -rf build *.egg-info

COPY tests ./tests
COPY scripts ./scripts
COPY docker-entrypoint.sh ./
# Strip CR first: checked out on Windows this file can arrive with CRLF, and
# a stray \r in the shebang makes the container die at startup with a very
# unhelpful "env: 'bash\r': No such file or directory". .gitattributes should
# prevent it, but this way the image builds correctly either way.
RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod +x docker-entrypoint.sh

EXPOSE 8000

CMD ["./docker-entrypoint.sh"]

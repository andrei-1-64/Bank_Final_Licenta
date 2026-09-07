# vision-service

Stateless image and PDF extraction for the BanK backend: OCR (ID cards,
IBANs), PDF text, and face embeddings.

## Why it is a separate service

Four features needed heavy native dependencies — `face-recognition` (dlib,
compiled from source), `pytesseract` (the system `tesseract-ocr` binary),
`pymupdf` and Pillow. They were the only reason the backend image carried
`cmake`, `build-essential` and the tesseract language packs, which made it
~1.7 GB and took roughly four minutes to build.

Moving them here means the backend rebuilds in seconds, and this image — the
one with the big attack surface and the slow build — has nothing worth
stealing in it.

## What it deliberately does NOT do

It is **not an authorisation boundary**. It has no database credentials, no
session handling, and no idea who the caller is acting for. It will read any
image it is handed; the backend decides whether it should have been handed
it. Every ownership, session, blocked-account and face-*comparison* check
stays in the backend.

This is why `face_auth` was split rather than moved: only turning a photo
into 128 numbers needs dlib. Storing that embedding, comparing two of them,
and issuing/consuming confirmation tokens are database work, and stayed in
`backend/app/modules/face_auth/service.py` — which is what lets
`enforce_face_confirmation` still be called in-process from transfers,
payments and proposals.

## Endpoints

All require the `X-Vision-Token` header (except `/health`).

| Method | Path                  | In                  | Out                                          |
|--------|-----------------------|---------------------|----------------------------------------------|
| POST   | `/v1/id-card`         | PNG                 | CNP, names, address, confidence, `raw_text`  |
| POST   | `/v1/iban`            | PNG / JPEG / PDF    | `iban`, confidence, `raw_text`               |
| POST   | `/v1/pdf-text`        | PDF                 | `text`, `page_count`                         |
| POST   | `/v1/face/embedding`  | JPEG / PNG          | `embedding` (128 floats)                     |
| GET    | `/health`             | —                   | `{"status": "ok"}`                           |

## Networking

No published port. It is reachable only from other services on the compose
network, at `http://vision:8100`. The shared token is defence in depth behind
that, not the primary control — and when it is unset the service refuses
every request rather than failing open.

## Known duplication

`app/validation.py` is a copy of
`backend/app/modules/auth/validation.py` — pure checksum/parsing functions
with no imports beyond the standard library. Both sides need them (the
backend for registration, this service to tell a real CNP/IBAN from OCR
noise). **If the checksum rules change, change both.** The alternative — a
shared installable package — was judged not worth a build step in two
Dockerfiles for 148 stable lines.

## Tests

    cd vision && pip install -e ".[dev]" && pytest

`tests/` holds the text-parsing tests that moved with the extractors. They
need neither tesseract nor an image: they run the parsers against known OCR
output.

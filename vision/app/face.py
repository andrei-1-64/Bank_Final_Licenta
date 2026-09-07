"""Face embedding extraction (dlib, via face_recognition), with blink-based
liveness detection.

This is the ONLY reason this service needs cmake and a C++ toolchain in its
image - and the reason it is a separate service at all. Everything the
banking backend does with faces afterwards (storing the embedding, comparing
two of them, issuing and consuming confirmation tokens) is plain arithmetic
and database work, and stays there.

Runs fully offline: photos are processed in this container and are never
persisted anywhere - the caller gets back 128 floats and nothing else.

LIVENESS. A single photo can't prove the camera saw a live person rather
than a printed photo or a photo of a phone screen - face_recognition just
matches pixels, it has no idea what it's looking at (see the old, single-
frame `extract_embedding`, kept below for callers that already have one
verified-live frame in hand). The defence here is a BLINK CHALLENGE:
`extract_embedding_with_liveness` takes a short burst of frames instead of
one photo, and requires the eyes to genuinely go open -> closed -> open
across that burst before it will hand back an embedding at all. A still
photo (or a photo held up to the camera) produces a flat eye-aspect-ratio
across every frame, since nothing in it is actually moving, so it never
manages the closed dip and is rejected before any face-matching happens.
This does NOT defend against a played VIDEO of the enrolled user blinking -
staging that is a materially harder attack than holding up a photo, and
catching it is out of scope for a demo-grade check like this one (see
face_auth/service.py's own caveat about what this biometric layer is and
isn't).
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import face_recognition


class NoFaceDetectedError(Exception):
    """No face in the photo at all."""


class MultipleFacesDetectedError(Exception):
    """More than one face - ambiguous, so the caller must not guess."""


class NoBlinkDetectedError(Exception):
    """The frame burst never showed eyes going open -> closed -> open -
    either a still photo/screen was held up to the camera, or the user
    genuinely didn't blink during the capture window. Either way, no
    embedding is returned: a burst that can't prove liveness is not
    downgraded to an ordinary single-frame match."""


#: Eye Aspect Ratio (Soukupová & Čech, 2016): the ratio of an eye's vertical
#: opening to its horizontal width, computed from 6 landmark points. Roughly
#: 0.3+ when open, collapsing toward 0 as the eyelids close. Two thresholds
#: with a gap between them (rather than one cutoff) avoid treating a single
#: borderline frame as a state change; the gap is deliberately generous
#: since input comes from a plain webcam, not a controlled lab camera.
_EAR_OPEN_THRESHOLD = 0.25
_EAR_CLOSED_THRESHOLD = 0.19

#: Fewer frames than this can't contain a genuine open-closed-open sequence
#: at any plausible capture rate. The frontend sends roughly 1.5-2s of
#: frames at ~6-7 fps, comfortably past this floor.
MIN_LIVENESS_FRAMES = 6


def _eye_aspect_ratio(eye_points: list[tuple[int, int]]) -> float:
    """`eye_points` is the 6-point (p1..p6) eye outline face_recognition
    returns, in dlib's standard order - EAR = (|p2-p6| + |p3-p5|) / (2*|p1-p4|)."""
    p1, p2, p3, p4, p5, p6 = eye_points
    vertical = math.dist(p2, p6) + math.dist(p3, p5)
    horizontal = math.dist(p1, p4)
    return vertical / (2.0 * horizontal)


def _detect_blink(ear_sequence: list[float]) -> bool:
    """True if the sequence dips below the closed threshold at some point
    with a genuinely-open frame on BOTH sides of that dip - i.e. eyes that
    were open, then closed, then open again, in that order. A flat sequence
    (a still photo, or a photo that never dips) never reaches the closed
    threshold at all and returns False immediately."""
    closed_index = next(
        (i for i, ear in enumerate(ear_sequence) if ear < _EAR_CLOSED_THRESHOLD), None
    )
    if closed_index is None:
        return False
    opened_before = any(ear > _EAR_OPEN_THRESHOLD for ear in ear_sequence[:closed_index])
    opened_after = any(ear > _EAR_OPEN_THRESHOLD for ear in ear_sequence[closed_index + 1 :])
    return opened_before and opened_after


def _locate_single_face(image_bytes: bytes):
    """Returns (face_location, loaded_image) for the one face in
    `image_bytes` - shared by every function below so face detection only
    ever runs once per frame, regardless of what gets extracted from it next.

    Goes through a temporary file because face_recognition.load_image_file
    wants a path; it is removed in the `finally` regardless of outcome, so a
    failed read never leaves a copy of someone's face on disk.
    """
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = Path(tmp.name)
    try:
        image = face_recognition.load_image_file(str(tmp_path))
        locations = face_recognition.face_locations(image)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not locations:
        raise NoFaceDetectedError()
    if len(locations) > 1:
        raise MultipleFacesDetectedError()
    return locations, image


def extract_embedding(image_bytes: bytes) -> list[float]:
    """Return the 128-value face encoding for the single face in
    `image_bytes`. No liveness check - for callers that already have a
    single, separately-verified-live frame in hand."""
    locations, image = _locate_single_face(image_bytes)
    encodings = face_recognition.face_encodings(image, locations)
    return encodings[0].tolist()


def extract_embedding_with_liveness(frames: list[bytes]) -> list[float]:
    """The blink-challenge entry point used by every real face capture
    (enroll, login, step-up confirm - see face_auth/service.py). Verifies
    the frame burst shows a real blink (NoBlinkDetectedError otherwise),
    then returns the embedding from whichever frame had the widest-open
    eyes - the sharpest, most front-on shot of the burst, and never the
    blurrier, half-closed blink frame itself.

    Raises NoFaceDetectedError/MultipleFacesDetectedError if ANY frame in
    the burst doesn't show exactly one face - a burst is only as trustworthy
    as its worst frame, so a partial capture is never salvaged.
    """
    if len(frames) < MIN_LIVENESS_FRAMES:
        raise NoBlinkDetectedError()

    ear_sequence: list[float] = []
    encodings: list[list[float]] = []
    for frame_bytes in frames:
        locations, image = _locate_single_face(frame_bytes)
        landmarks = face_recognition.face_landmarks(image, locations)[0]
        ear = (
            _eye_aspect_ratio(landmarks["left_eye"]) + _eye_aspect_ratio(landmarks["right_eye"])
        ) / 2.0
        ear_sequence.append(ear)
        encodings.append(face_recognition.face_encodings(image, locations)[0].tolist())

    if not _detect_blink(ear_sequence):
        raise NoBlinkDetectedError()

    widest_open_index = ear_sequence.index(max(ear_sequence))
    return encodings[widest_open_index]

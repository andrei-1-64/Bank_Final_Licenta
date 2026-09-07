"""Unit tests for the blink-based liveness check (see face.py's module
docstring for the why). These exercise the pure math (_eye_aspect_ratio,
_detect_blink) directly with synthetic points/sequences - no real image or
face_recognition call needed, since neither of those functions touches
either.
"""

import pytest

from app.face import (
    MIN_LIVENESS_FRAMES,
    NoBlinkDetectedError,
    _detect_blink,
    _eye_aspect_ratio,
    extract_embedding_with_liveness,
)


def test_eye_aspect_ratio_of_a_clearly_open_eye_is_above_the_open_threshold():
    # A roughly almond-shaped open eye: tall relative to its width.
    open_eye = [(0, 0), (2, -3), (4, -3), (6, 0), (4, 3), (2, 3)]
    assert _eye_aspect_ratio(open_eye) > 0.25


def test_eye_aspect_ratio_of_a_closed_eye_is_below_the_closed_threshold():
    # A closed eye: nearly flat, almost no vertical extent.
    closed_eye = [(0, 0), (2, -0.2), (4, -0.2), (6, 0), (4, 0.2), (2, 0.2)]
    assert _eye_aspect_ratio(closed_eye) < 0.19


def test_a_flat_sequence_never_detects_a_blink():
    """The exact scenario a spoofed still photo produces: the same EAR on
    every frame, since nothing in a still image ever actually moves - this
    is what used to let a photo held up to the camera pass as a live face."""
    flat_open = [0.30] * 10
    assert _detect_blink(flat_open) is False

    flat_mid = [0.22] * 10
    assert _detect_blink(flat_mid) is False


def test_a_genuine_open_closed_open_sequence_is_detected_as_a_blink():
    sequence = [0.30, 0.29, 0.28, 0.15, 0.10, 0.16, 0.28, 0.30, 0.29, 0.30]
    assert _detect_blink(sequence) is True


def test_a_dip_with_no_open_frame_before_it_is_not_a_blink():
    """Eyes that start already closed and only open once never prove they
    were open BEFORE closing - not the required open -> closed -> open
    shape, just half of it."""
    sequence = [0.10, 0.10, 0.12, 0.28, 0.30, 0.29]
    assert _detect_blink(sequence) is False


def test_a_dip_with_no_open_frame_after_it_is_not_a_blink():
    sequence = [0.30, 0.29, 0.28, 0.15, 0.10, 0.12]
    assert _detect_blink(sequence) is False


def test_too_few_frames_is_rejected_before_any_image_is_decoded():
    """The frame-count floor is checked first - a burst this short can't
    possibly contain a genuine blink at any plausible capture rate, so it's
    refused before wasting a face-detection pass on it (the garbage bytes
    below would raise a decode error, not NoBlinkDetectedError, if the
    function tried)."""
    with pytest.raises(NoBlinkDetectedError):
        extract_embedding_with_liveness([b"not-a-real-image"] * (MIN_LIVENESS_FRAMES - 1))

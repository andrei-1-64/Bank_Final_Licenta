"""enforce_face_confirmation's `require_enrolled` gate (Step 16, item 3).

Not exercised through the HTTP layer - no existing route passes
`require_enrolled=True` yet (it is a capability for a future caller, added
without touching transfers/payments). These call the service function
directly against the real test database, the same way
face_auth/service.py::has_face_enrolled needs a real face_credentials row
(or the deliberate absence of one) to answer.
"""

import pytest

from app.core.exceptions import (
    FaceConfirmationRequiredError,
    FaceEnrollmentRequiredError,
    UnauthorizedError,
)
from app.modules.face_auth import service as face_auth_service


async def test_require_enrolled_false_no_ops_for_an_unenrolled_user(user_factory, supabase):
    """Existing behaviour, unchanged: `required=False` and the new parameter
    left at its default both mean "nothing to check here"."""
    user = await user_factory()

    await face_auth_service.enforce_face_confirmation(
        supabase, user, required=False, token=None
    )


async def test_require_enrolled_true_rejects_an_unenrolled_user(user_factory, supabase):
    user = await user_factory()

    with pytest.raises(FaceEnrollmentRequiredError):
        await face_auth_service.enforce_face_confirmation(
            supabase, user, required=False, token=None, require_enrolled=True
        )


async def test_require_enrolled_true_still_asks_for_a_token_when_required(
    user_factory, supabase, enroll_face
):
    """`require_enrolled=True` on an enrolled user is not itself a pass - it
    only clears the enrollment precondition. The normal `required`/`token`
    flow underneath still runs: no token supplied still asks for one."""
    user = await user_factory()
    await enroll_face(user.id)  # enrolled; the returned token is unused here

    with pytest.raises(FaceConfirmationRequiredError):
        await face_auth_service.enforce_face_confirmation(
            supabase, user, required=True, token=None, require_enrolled=True
        )


async def test_require_enrolled_true_proceeds_to_normal_flow_for_an_enrolled_user(
    user_factory, supabase, enroll_face
):
    """With a real, freshly-issued token the call goes all the way through
    and consumes it - the same "normal flow" an existing caller gets from
    `required=True` alone; `require_enrolled=True` changes nothing once
    enrollment is confirmed."""
    user = await user_factory()
    token = await enroll_face(user.id)

    await face_auth_service.enforce_face_confirmation(
        supabase, user, required=True, token=token, require_enrolled=True
    )


# ---------------------------------------------------------------------------
# The password fallback - an equal alternative to a face token, not a
# weaker check. user_factory always seeds "password123" (see conftest.py).
# ---------------------------------------------------------------------------


async def test_correct_password_satisfies_a_required_confirmation(
    user_factory, supabase, enroll_face
):
    user = await user_factory()
    await enroll_face(user.id)  # enrollment is still a precondition either way

    await face_auth_service.enforce_face_confirmation(
        supabase, user, required=True, token=None, password="password123"
    )


async def test_wrong_password_is_rejected(user_factory, supabase, enroll_face):
    user = await user_factory()
    await enroll_face(user.id)

    with pytest.raises(UnauthorizedError):
        await face_auth_service.enforce_face_confirmation(
            supabase, user, required=True, token=None, password="not-the-password"
        )


async def test_password_does_not_help_an_unenrolled_user(user_factory, supabase):
    """A correct password is not a substitute for enrollment - there is
    still no face credential on file at all, same reasoning as the
    token-based FaceEnrollmentRequiredError case above."""
    user = await user_factory()

    with pytest.raises(FaceEnrollmentRequiredError):
        await face_auth_service.enforce_face_confirmation(
            supabase, user, required=True, token=None, password="password123"
        )


async def test_neither_token_nor_password_still_asks_for_confirmation(
    user_factory, supabase, enroll_face
):
    """Existing behaviour, unchanged: omitting both stays a plain
    "step-up needed" response, not an error about which method to use."""
    user = await user_factory()
    await enroll_face(user.id)

    with pytest.raises(FaceConfirmationRequiredError):
        await face_auth_service.enforce_face_confirmation(
            supabase, user, required=True, token=None, password=None
        )


async def test_a_valid_token_wins_over_a_password_if_both_are_somehow_given(
    user_factory, supabase, enroll_face
):
    """Not a real client scenario (the frontend only ever sends one), but
    the precedence should be deterministic rather than accidental."""
    user = await user_factory()
    token = await enroll_face(user.id)

    await face_auth_service.enforce_face_confirmation(
        supabase, user, required=True, token=token, password="wrong-password-ignored"
    )

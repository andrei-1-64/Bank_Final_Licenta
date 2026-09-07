"""Typed application errors.

Every error the app raises on purpose should be an AppError subclass so
core/middleware.py can map it to a consistent JSON body and status code
instead of leaking a stack trace. Anything that reaches FastAPI as a plain
Exception is treated as a bug and mapped to a generic 500.
"""


class AppError(Exception):
    status_code = 500
    error_code = "internal_error"
    default_message = "Something went wrong."

    def __init__(self, message: str | None = None, *, details: dict | None = None):
        self.message = message or self.default_message
        #: Optional structured payload alongside the message (see
        #: core/middleware.py's handle_app_error) - for callers that need
        #: more than a string to render a proper response, e.g.
        #: SubscriptionPriceIncreaseError's old/new amounts below.
        self.details = details
        super().__init__(self.message)


class ValidationError(AppError):
    status_code = 422
    error_code = "validation_error"
    default_message = "Invalid input."


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    default_message = "Resource not found."


class AccountNotFoundError(NotFoundError):
    error_code = "account_not_found"
    default_message = "Account not found."


class IbanNotFoundError(NotFoundError):
    error_code = "iban_not_found"
    default_message = "No account was found for that IBAN."


class ConversationNotFoundError(NotFoundError):
    error_code = "conversation_not_found"
    default_message = "Conversation not found."


class AccountClosedError(AppError):
    status_code = 409
    error_code = "account_closed"
    default_message = "Account is closed."


class AccountNotEmptyError(AppError):
    status_code = 409
    error_code = "account_not_empty"
    default_message = "Account balance must be zero before it can be closed."


class CurrencyMismatchError(AppError):
    status_code = 422
    error_code = "currency_mismatch"
    default_message = "Currencies do not match."


class ExchangeRateUnavailableError(AppError):
    """No BNR rate could be obtained for a cross-currency transfer.

    503 rather than 422: nothing about the request is wrong, an upstream we
    depend on is simply not answering and has no cached answer either. The
    only alternative would be inventing a rate, which is never an option -
    see app/core/bnr_client.py.
    """

    status_code = 503
    error_code = "exchange_rate_unavailable"
    default_message = "Cursul valutar BNR nu este disponibil momentan."


class SubscriptionPriceIncreaseError(AppError):
    """Raised by payments/service.py when a payment to a recognized
    recurring merchant costs more than the sender's last payment to that
    same IBAN - see _detect_subscription_price_increase. `details` carries
    previous_amount_minor/new_amount_minor/currency/beneficiary_name(/
    website) so the frontend can show the actual numbers, not just this
    generic message. Retrying the same payload with confirm_price_increase
    = true bypasses this - it's a warning, not a hard block."""

    status_code = 409
    error_code = "subscription_price_increase"
    default_message = "This subscription's price appears to have gone up since your last payment."


class InsufficientFundsError(AppError):
    status_code = 422
    error_code = "insufficient_funds"
    default_message = "Insufficient funds."


class InvalidLedgerLegsError(AppError):
    status_code = 422
    error_code = "invalid_ledger_legs"
    default_message = "Ledger legs are not balanced."


class UnauthorizedError(AppError):
    status_code = 401
    error_code = "unauthorized"
    default_message = "Authentication required."


class ForbiddenError(AppError):
    status_code = 403
    error_code = "forbidden"
    default_message = "Not allowed to access this resource."


class AccountBlockedError(AppError):
    """The credentials are valid; the account itself has been disabled by an
    admin. Distinct from UnauthorizedError on purpose - "wrong password" and
    "your account is blocked" are different situations, and the user can do
    nothing about the second by retrying."""

    status_code = 403
    error_code = "account_blocked"
    default_message = "This account has been blocked. Contact support."


class InvalidIdempotencyKeyError(AppError):
    status_code = 400
    error_code = "invalid_idempotency_key"
    default_message = "Missing or invalid Idempotency-Key header."


class IdempotencyKeyConflictError(AppError):
    status_code = 409
    error_code = "idempotency_key_conflict"
    default_message = "This Idempotency-Key was already used for a different request."


class RateLimitExceededError(AppError):
    status_code = 429
    error_code = "rate_limit_exceeded"
    default_message = "Too many requests."


class EmailAlreadyRegisteredError(AppError):
    status_code = 409
    error_code = "email_already_registered"
    default_message = "An account with this email already exists."


class NationalIdAlreadyRegisteredError(AppError):
    status_code = 409
    error_code = "national_id_already_registered"
    default_message = "An account associated with this national ID already exists."


class LoginRateLimitedError(AppError):
    status_code = 429
    error_code = "login_rate_limited"
    default_message = "Too many failed login attempts. Try again later."


class ProposalRateLimitedError(AppError):
    """Too many wrong confirm attempts on one proposal (Step 16, item 5) -
    see chat/proposals_service.py::confirm_proposal. Same shape as
    LoginRateLimitedError, keyed on (proposal_id, user_id) instead of email:
    the 10-minute proposal expiry bounds the window on its own, but without
    this a single pending proposal could still absorb hundreds of guesses
    per second."""

    status_code = 429
    error_code = "proposal_rate_limited"
    default_message = "Prea multe încercări. Propunerea a fost blocată."


class InvalidResetCodeError(AppError):
    status_code = 400
    error_code = "invalid_reset_code"
    default_message = "Invalid or expired reset code."


class InvalidSigningCodeError(AppError):
    """The OTP half of an admin-document signature (see
    app/modules/esign/service.py::request_signing_code /
    confirm_admin_document) was wrong, expired, already used, or never
    requested. Deliberately as vague as InvalidResetCodeError - which of
    those it was is not useful to an attacker guessing codes."""

    status_code = 400
    error_code = "invalid_signing_code"
    default_message = "Invalid or expired signing code."


class FaceConfirmationRequiredError(AppError):
    """This amount needs step-up face auth first - see
    app/modules/face_auth::enforce_face_confirmation. 428 Precondition
    Required is the semantically correct status for "do X first, then
    retry", distinct from a plain validation failure."""

    status_code = 428
    error_code = "face_confirmation_required"
    default_message = "This amount requires face confirmation before it can proceed."


class FaceEnrollmentRequiredError(AppError):
    """This action requires step-up face auth, but the user has never
    enrolled Face ID at all - so there is no token they could possibly
    supply. Distinct from FaceConfirmationRequiredError (enrolled, just
    needs a fresh token this request): the fix here is "go enroll Face ID
    first", not "retry with a token". See enforce_face_confirmation."""

    status_code = 428
    error_code = "face_enrollment_required"
    default_message = "This action requires Face ID to be enrolled first."


class InvalidFaceConfirmationError(AppError):
    status_code = 400
    error_code = "invalid_face_confirmation"
    default_message = "Invalid or expired face confirmation."


class FaceAuthMethodRequiredError(AppError):
    """The proposal being confirmed is a transfer/payment that WOULD have
    required Face ID (not password) on the direct, non-AI path - see
    proposals_service._proposal_requires_face and
    face_auth_service.enforce_face_confirmation. Distinct from
    FaceConfirmationRequiredError: the caller isn't missing a token, they
    affirmatively chose the wrong step-up method for this amount/recipient.
    Not a hard wall, though: proposals_service.confirm_proposal stops
    raising this once enough failed attempts are already on record for this
    proposal (FACE_FAILURES_BEFORE_PASSWORD_FALLBACK) - the caller should
    retry with `auth_method: "face"` UNLESS the UI already walked them
    through several failed face attempts, in which case a plain password
    retry is expected to succeed instead."""

    status_code = 428
    error_code = "face_auth_method_required"
    default_message = "This proposal requires Face ID confirmation - password is not enough."


class InvalidEnrollmentCodeError(AppError):
    status_code = 400
    error_code = "invalid_enrollment_code"
    default_message = "Invalid or expired verification code."


class DeviceNotTrustedError(AppError):
    status_code = 401
    error_code = "device_not_trusted"
    default_message = "This device is not enrolled."


class TermDepositLockedError(AppError):
    status_code = 409
    error_code = "term_deposit_locked"
    default_message = "This account is locked until its maturity date."


class ProposalNotPendingError(AppError):
    """The proposal was already confirmed, rejected, or expired - see
    app/modules/chat/proposals_service.py. 409 Conflict, same reasoning as
    IdempotencyKeyConflictError: the request isn't invalid, it's just too
    late to apply."""

    status_code = 409
    error_code = "proposal_not_pending"
    default_message = "This proposal has already been confirmed, rejected, or expired."


class ProposalExpiredError(AppError):
    """The proposal is past its confirmation window (see
    PROPOSAL_EXPIRY_MINUTES) - a more specific case of "not pending" so the
    frontend can show a distinct message ("Propunerea a expirat.") rather
    than a generic conflict."""

    status_code = 409
    error_code = "proposal_expired"
    default_message = "This proposal has expired."


class AIServiceUnavailableError(AppError):
    """The AI layer could not be constructed - typically missing credentials.

    Deliberately vague to the caller: which piece of configuration is absent is
    an operator's concern, not something to advertise over HTTP.
    """

    status_code = 503
    error_code = "ai_service_unavailable"
    default_message = "AI service unavailable"


class AIProviderError(AppError):
    """The upstream model call failed (network, auth, quota, ...)."""

    status_code = 502
    error_code = "ai_provider_error"
    default_message = "AI model request failed"


class ESignUnavailableError(AppError):
    """ESIGN_PRIVATE_KEY is unset or unusable - see
    app/modules/esign/keys.py::_load_private_key. Fails closed rather than
    generating a throwaway key at import time, same reasoning as
    AIServiceUnavailableError."""

    status_code = 503
    error_code = "esign_unavailable"
    default_message = "Electronic signing is not available right now."


class AIProviderMisconfiguredError(AppError):
    """AI_PROVIDER names something the app has no implementation for.

    A deployment mistake rather than a caller mistake, so it is a 500 - but the
    message names the valid values, since only an operator will ever see it.
    """

    status_code = 500
    error_code = "ai_provider_misconfigured"
    default_message = "AI provider is misconfigured"


class SpeechServiceUnavailableError(AppError):
    """AZURE_SPEECH_* is unset or incomplete - see
    Settings.require_azure_speech(). Fails closed rather than silently
    returning no audio, same reasoning as AIServiceUnavailableError; the
    frontend falls back to the browser's own SpeechSynthesis on this."""

    status_code = 503
    error_code = "speech_service_unavailable"
    default_message = "Read-aloud is not available right now."


class SpeechProviderError(AppError):
    """The upstream Azure Speech call failed (network, auth, quota, ...)."""

    status_code = 502
    error_code = "speech_provider_error"
    default_message = "Text-to-speech request failed"

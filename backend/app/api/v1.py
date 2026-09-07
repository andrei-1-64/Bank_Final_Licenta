from fastapi import APIRouter

from app.modules.accounts.router import router as accounts_router
from app.modules.admin.router import router as admin_router
from app.modules.auth.router import router as auth_router
from app.modules.beneficiaries.router import router as beneficiaries_router
from app.modules.card_orders.router import router as card_orders_router
from app.modules.cards.router import router as cards_router
from app.modules.chat.router import router as chat_router
from app.modules.documents.router import router as documents_router
from app.modules.esign.router import router as esign_router
from app.modules.face_auth.router import router as face_auth_router
from app.modules.iban_ocr.router import router as iban_ocr_router
from app.modules.id_ocr.router import router as id_ocr_router
from app.modules.insights.router import router as insights_router
from app.modules.notifications.router import router as notifications_router
from app.modules.onboarding.router import router as onboarding_router
from app.modules.payments.router import router as payments_router
from app.modules.scheduled_transfers.router import router as scheduled_transfers_router
from app.modules.speech.router import router as speech_router
from app.modules.statements.router import router as statements_router
from app.modules.transactions.router import router as transactions_router
from app.modules.transfers.router import router as transfers_router
from app.modules.trusted_devices.router import router as trusted_devices_router
from app.modules.users.router import router as users_router

api_router = APIRouter()

api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(accounts_router, prefix="/accounts", tags=["accounts"])
api_router.include_router(transactions_router, prefix="/accounts", tags=["transactions"])
api_router.include_router(transfers_router, prefix="/transfers", tags=["transfers"])
api_router.include_router(
    scheduled_transfers_router, prefix="/scheduled-transfers", tags=["scheduled-transfers"]
)
api_router.include_router(cards_router, prefix="/cards", tags=["cards"])
api_router.include_router(card_orders_router, prefix="/card-orders", tags=["card-orders"])
api_router.include_router(payments_router, prefix="/payments", tags=["payments"])
api_router.include_router(beneficiaries_router, prefix="/beneficiaries", tags=["beneficiaries"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(chat_router, prefix="/chat", tags=["chat"])
api_router.include_router(documents_router, prefix="/documents", tags=["documents"])
api_router.include_router(esign_router, prefix="/esign", tags=["esign"])
api_router.include_router(statements_router, prefix="/statements", tags=["statements"])
api_router.include_router(id_ocr_router, prefix="/id-ocr", tags=["id-ocr"])
api_router.include_router(iban_ocr_router, prefix="/iban-ocr", tags=["iban-ocr"])
api_router.include_router(onboarding_router, prefix="/onboarding", tags=["onboarding"])
api_router.include_router(notifications_router, prefix="/notifications", tags=["notifications"])
api_router.include_router(face_auth_router, prefix="/auth/face", tags=["face-auth"])
api_router.include_router(insights_router, prefix="/insights", tags=["insights"])
api_router.include_router(speech_router, prefix="/speech", tags=["speech"])
api_router.include_router(
    trusted_devices_router, prefix="/trusted-devices", tags=["trusted-devices"]
)
# Gated as a whole by require_admin, declared on the router itself (see
# app/modules/admin/router.py) - not per endpoint.
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from supabase import AsyncClient

from app.core.dependencies import get_current_user
from app.db.supabase_client import get_supabase
from app.modules.accounts import service, statement_service
from app.modules.accounts.schemas import (
    AccountCreate,
    AccountHolderRead,
    AccountProductsRead,
    AccountRead,
    TermDepositOption,
)
from app.modules.ledger import service as ledger_service
from app.modules.scheduled_transfers import service as scheduled_transfers_service
from app.modules.users.schemas import UserRead

router = APIRouter()


async def _to_read_model(supabase: AsyncClient, account: dict) -> AccountRead:
    # Lazy accrual: a savings/term-deposit account's balance always
    # reflects interest earned so far, computed on every read rather than
    # on a schedule - see accounts/service.py::accrue_interest_if_due.
    await service.accrue_interest_if_due(supabase, account)
    balance_minor = await ledger_service.get_balance(supabase, uuid.UUID(account["id"]))
    return AccountRead(
        id=account["id"],
        name=account["name"],
        currency=account["currency"],
        iban=account.get("iban"),
        status=account["status"],
        balance_minor=balance_minor,
        product_type=account.get("product_type", "checking"),
        interest_rate_bps=account.get("interest_rate_bps"),
        term_months=account.get("term_months"),
        maturity_date=account.get("maturity_date"),
        created_at=account["created_at"],
    )


@router.post("", response_model=AccountRead, status_code=201)
async def open_account(
    payload: AccountCreate,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> AccountRead:
    account = await service.open_account(
        supabase,
        user,
        payload.name,
        payload.currency,
        product_type=payload.product_type,
        term_months=payload.term_months,
    )
    return await _to_read_model(supabase, account)


@router.get("", response_model=list[AccountRead])
async def list_accounts(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[AccountRead]:
    # Lazy execution: any scheduled transfer due for this user fires here,
    # before balances are read - same "no cron" pattern as interest accrual
    # above. See scheduled_transfers/service.py's module docstring.
    await scheduled_transfers_service.run_due_transfers_for_owner(supabase, str(user.id))
    accounts = await service.list_accounts(supabase, user)
    return [await _to_read_model(supabase, account) for account in accounts]


@router.get("/by-iban/{iban}", response_model=AccountHolderRead)
async def get_account_holder(
    iban: str,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> AccountHolderRead:
    holder = await service.get_account_holder_by_iban(supabase, iban)
    return AccountHolderRead(**holder)


@router.get("/products", response_model=AccountProductsRead)
async def get_account_products() -> AccountProductsRead:
    return AccountProductsRead(
        savings_interest_rate_bps=service.SAVINGS_INTEREST_RATE_BPS,
        term_deposit_options=[
            TermDepositOption(term_months=months, interest_rate_bps=rate_bps)
            for months, rate_bps in sorted(service.TERM_DEPOSIT_RATES_BPS.items())
        ],
    )


@router.get("/{account_id}", response_model=AccountRead)
async def get_account(
    account_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> AccountRead:
    account = await service.get_account(supabase, user, account_id)
    return await _to_read_model(supabase, account)


@router.get("/{account_id}/statement/pdf")
async def get_account_statement_pdf(
    account_id: uuid.UUID,
    period_start: date | None = Query(default=None),
    period_end: date | None = Query(default=None),
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> Response:
    """Downloadable PDF statement for one account - see
    statement_service.generate_statement_pdf for the balance math and
    statement_pdf.py for the rendering. Defaults to the last 30 days when
    no period is given, so a plain "download my statement" click works
    without the frontend having to compute dates itself."""
    resolved_end = period_end or date.today()
    resolved_start = period_start or (resolved_end - timedelta(days=30))

    pdf_bytes = await statement_service.generate_statement_pdf(
        supabase, user, account_id, period_start=resolved_start, period_end=resolved_end
    )
    filename = f"extras-cont-{resolved_start.isoformat()}-{resolved_end.isoformat()}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{account_id}/close", response_model=AccountRead)
async def close_account(
    account_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> AccountRead:
    account = await service.close_account(supabase, user, account_id)
    return await _to_read_model(supabase, account)

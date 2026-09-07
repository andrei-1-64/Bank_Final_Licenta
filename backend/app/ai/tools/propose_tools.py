"""The five write-adjacent tools: propose_transfer, propose_payment,
propose_open_account, propose_close_account, propose_cancel_card.

None of these move money, open/close an account, or cancel a card. Each one
only ever inserts a `pending` row into `proposals` (see
app/modules/chat/proposals_service.py::create_proposal) and hands the model a
human-readable summary to relay. The real action only ever happens later, from
`proposals_service.confirm_proposal`, after the user has proven their identity
via step-up auth (face token or password) - never from here.

`read_only = False` on every tool in this file: it is the first time that flag
is set anywhere in the codebase (see app/ai/tools/base.py's docstring, written
back when "no write tools exist yet"). It marks "this tool has a side effect",
not "this tool moves money" - the side effect is always just a proposal row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, model_validator

from app.ai.context import AccessDeniedError, Context, IdentityError
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.core import bnr_client, fx
from app.core.exceptions import IbanNotFoundError

if TYPE_CHECKING:
    from supabase import AsyncClient

logger = logging.getLogger(__name__)

#: Every tool name in this file - chat/router.py uses this to recognise a
#: propose_* tool result in the trace and attach the proposal to ChatResponse.
PROPOSE_TOOL_NAMES = frozenset(
    {
        "propose_transfer",
        "propose_payment",
        "propose_open_account",
        "propose_close_account",
        "propose_cancel_card",
    }
)

_PRODUCT_LABELS_RO = {
    "checking": "curent",
    "savings": "economii",
    "term_deposit": "depozit la termen",
}


def _format_amount(amount_minor: int, currency: str) -> str:
    """Romanian formatting, same convention as BankingAgent's SYSTEM_PROMPT:
    comma decimal separator, two decimals. 50000 RON -> "500,00 RON"."""
    return f"{amount_minor / 100:.2f}".replace(".", ",") + f" {currency}"


async def _resolve_owned_account(
    supabase: AsyncClient, context: Context, account_id: str
) -> dict:
    """The ONLY place an account is decided, same pattern as every read tool:
    `context.resolve_account` can only narrow to something the context user
    already owns, never widen. Raises IdentityError (never leaking which
    account was refused) rather than returning something the caller might
    mistake for success."""
    from app.modules.accounts import service as accounts_service

    resolved_id = context.resolve_account(account_id)
    return await accounts_service.get_account_for_owner(supabase, context.user_id, resolved_id)


async def _insufficient_funds_error(
    supabase: AsyncClient, tool_name: str, account: dict, amount_minor: int
) -> ToolResult | None:
    """Checked BEFORE a proposal is created, not just at execution time - a
    proposal for an amount the source account can't cover used to sail
    through step-up confirmation (Face ID/password) and only fail
    afterwards, with a raw "insufficient funds" error the user had already
    committed to. Returns a failure ToolResult if the balance is too low,
    None otherwise (this is a snapshot check, not a lock - the real,
    authoritative check still runs again at execution time, inside the same
    RPC transaction that moves the money)."""
    import uuid

    from app.modules.ledger import service as ledger_service

    balance = await ledger_service.get_balance(supabase, uuid.UUID(account["id"]))
    if balance >= amount_minor:
        return None
    return ToolResult.failure(
        name=tool_name,
        error=(
            f"Fonduri insuficiente în {account['name']}: sold disponibil "
            f"{_format_amount(balance, account['currency'])}, sumă cerută "
            f"{_format_amount(amount_minor, account['currency'])}."
        ),
    )


@dataclass(frozen=True)
class _Conversion:
    """A cross-currency transfer's numbers, locked at proposal time."""

    original_amount_minor: int
    original_currency: str
    converted_amount_minor: int
    converted_currency: str
    exchange_rate: Decimal
    exchange_rate_date: date
    stale: bool

    def as_proposal_columns(self) -> dict:
        """The six `proposals` columns 0023 added, as PostgREST wants them.

        `exchange_rate` goes as a STRING: the payload is serialised as JSON,
        which has no decimal type, and letting a Decimal become a float here
        would be the one place in this path where the rate stops being exact.
        Postgres parses it back into NUMERIC on the way in.
        """
        return {
            "original_amount_minor": self.original_amount_minor,
            "original_currency": self.original_currency,
            "converted_amount_minor": self.converted_amount_minor,
            "converted_currency": self.converted_currency,
            "exchange_rate": str(self.exchange_rate),
            "exchange_rate_date": self.exchange_rate_date.isoformat(),
        }

    def summary_suffix(self) -> str:
        """The Romanian sentence the user reads before confirming.

        Shows BOTH amounts, the rate and BNR's publication date, because the
        number that leaves their account and the number that arrives are
        different and they are agreeing to both.
        """
        rate = f"{self.exchange_rate:.4f}".replace(".", ",")
        date_ro = self.exchange_rate_date.strftime("%d.%m.%Y")
        text = (
            f" (≈ {fx.format_minor(self.converted_amount_minor, self.converted_currency)} "
            f"la cursul BNR de {rate} din {date_ro})"
        )
        if self.stale:
            text += (
                " — atenție: nu am putut contacta BNR acum, acesta este "
                "ultimul curs cunoscut."
            )
        return text


async def _convert_for_transfer(
    from_account: dict, to_account: dict, amount_minor: int
) -> _Conversion | None:
    """The BNR conversion for a cross-currency transfer, or None if the two
    accounts already share a currency.

    Returns None for the overwhelmingly common same-currency case WITHOUT
    touching `bnr_client` at all - no fetch, no cache read, nothing. That
    path has to stay exactly what it was.

    Raises `BnrUnavailableError` (cold cache) or `UnsupportedCurrencyError`
    for the caller to turn into a Romanian tool error. Deliberately does not
    swallow either: a transfer must never be proposed at a rate nobody
    published.
    """
    if from_account["currency"] == to_account["currency"]:
        return None

    rates, stale = await bnr_client.get_rates()
    rate = fx.rate_between(rates, from_account["currency"], to_account["currency"])
    return _Conversion(
        original_amount_minor=amount_minor,
        original_currency=from_account["currency"],
        converted_amount_minor=fx.convert_minor(amount_minor, rate),
        converted_currency=to_account["currency"],
        exchange_rate=rate,
        exchange_rate_date=rates.published_on,
        stale=stale,
    )


def _conversion_unavailable_error(tool_name: str, exc: Exception) -> ToolResult:
    """No rate, so no proposal. Never a fabricated one.

    An invented exchange rate is indistinguishable from a real one once it is
    on a proposal the user is about to confirm, so the only honest outcome
    here is to say the transfer cannot be prepared right now.
    """
    if isinstance(exc, fx.UnsupportedCurrencyError):
        return ToolResult.failure(
            name=tool_name,
            error=(
                f"BNR nu publică un curs pentru {exc}, așa că nu pot pregăti "
                "acest transfer valutar."
            ),
        )
    return ToolResult.failure(
        name=tool_name,
        error=(
            "Cursul valutar BNR nu este disponibil momentan, așa că nu pot "
            "pregăti un transfer între conturi cu monede diferite. Te rog "
            "încearcă din nou peste câteva minute."
        ),
    )


class ProposeTransferInput(BaseModel):
    from_account_id: str = Field(description="One of the user's own account identifiers.")
    to_account_id: str = Field(description="Another of the user's own account identifiers.")
    amount_minor: int = Field(gt=0, description="Amount in integer minor units (e.g. cents).")
    #: ADVISORY. The currency actually written onto the proposal is read from
    #: the source account server-side, never from here - see `run`. Kept in the
    #: schema because the model has always sent it and dropping the field
    #: would make every existing prompt example invalid, but a wrong guess
    #: here can no longer produce a proposal that fails at confirmation time.
    currency: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description=(
            "Optional. The account's own currency is used regardless - you do "
            "not need to know or guess it."
        ),
    )
    description: str | None = Field(default=None, max_length=500)


class ProposeTransferTool(Tool):
    name = "propose_transfer"
    description = (
        "Pregătește o propunere de transfer între conturile proprii ale "
        "utilizatorului. Transferul NU se execută - utilizatorul trebuie să "
        "confirme, cu Face ID sau parolă, înainte ca banii să se miște. "
        "Apelează list_accounts înainte, ca să obții id-uri reale de cont - "
        "nu ghici și nu inventa niciodată un id de cont."
    )
    input_schema = ProposeTransferInput
    read_only = False

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ProposeTransferInput)

        try:
            from_account = await _resolve_owned_account(
                self._supabase, context, validated_input.from_account_id
            )
            to_account = await _resolve_owned_account(
                self._supabase, context, validated_input.to_account_id
            )
        except IdentityError:
            logger.warning(
                "access denied user_id=%s tool=%s", context.user_id, self.name
            )
            raise

        insufficient_funds = await _insufficient_funds_error(
            self._supabase, self.name, from_account, validated_input.amount_minor
        )
        if insufficient_funds is not None:
            return insufficient_funds

        # THE currency, from the account rather than from the model. This is
        # the currency that LEAVES - `validated_input.currency` is advisory
        # and deliberately not consulted, since a model that guessed "RON"
        # for a EUR account used to produce a proposal that passed review,
        # passed Face ID, and only then failed in transfers.service.
        currency = from_account["currency"]

        # None for a same-currency transfer, and BNR is not touched at all in
        # that case (see _convert_for_transfer).
        try:
            conversion = await _convert_for_transfer(
                from_account, to_account, validated_input.amount_minor
            )
        except (bnr_client.BnrUnavailableError, fx.UnsupportedCurrencyError) as exc:
            logger.warning("propose_transfer: no BNR rate available (%s)", type(exc).__name__)
            return _conversion_unavailable_error(self.name, exc)

        amount_str = _format_amount(validated_input.amount_minor, currency)
        summary = (
            f"Transfer de {amount_str} din {from_account['name']} în {to_account['name']}"
        )
        if conversion is not None:
            summary += conversion.summary_suffix()

        proposal = await self._create_proposal(
            context, validated_input, summary, currency, conversion
        )
        return ToolResult(
            name=self.name, data={"proposal_id": proposal["id"], "summary": summary}
        )

    async def _create_proposal(
        self,
        context: Context,
        validated_input: ProposeTransferInput,
        summary: str,
        currency: str,
        conversion: _Conversion | None,
    ) -> dict:
        from app.modules.chat.proposals_service import create_proposal

        return await create_proposal(
            self._supabase,
            user_id=context.user_id,
            conversation_id=_require_conversation(context, self.name),
            proposal_type="transfer",
            payload={
                "from_account_id": validated_input.from_account_id,
                "to_account_id": validated_input.to_account_id,
                "amount_minor": validated_input.amount_minor,
                "currency": currency,
                "description": validated_input.description,
            },
            summary=summary,
            # None for a same-currency transfer, and `create_proposal` then
            # sends an insert with exactly the columns it always sent - so
            # the ordinary path keeps working whether or not 0023 has been
            # applied yet.
            conversion=conversion.as_proposal_columns() if conversion else None,
        )


class ProposePaymentInput(BaseModel):
    from_account_id: str = Field(description="One of the user's own account identifiers.")
    to_iban: str = Field(min_length=15, max_length=34)
    beneficiary_name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "The recipient's name, as the user believes it to be. Advisory only - "
            "this tool independently re-resolves the real account holder from the "
            "IBAN and uses THAT name on the proposal, never this one, so a wrong "
            "assumption here can't end up on a real payment."
        ),
    )
    amount_minor: int = Field(gt=0, description="Amount in integer minor units (e.g. cents).")
    description: str | None = Field(default=None, max_length=500)


class ProposePaymentTool(Tool):
    name = "propose_payment"
    description = (
        "Pregătește o propunere de plată către un alt cont prin IBAN. Plata "
        "NU se execută - utilizatorul trebuie să confirme, cu Face ID sau "
        "parolă. Apelează list_accounts înainte pentru contul sursă, și "
        "resolve_iban_holder înainte pentru IBAN-ul destinatarului - arată "
        "numele real găsit utilizatorului și cere-i să confirme că e persoana "
        "potrivită, înainte de a apela acest tool. Nu ghici și nu inventa "
        "niciodată un id de cont sau un nume de titular."
    )
    input_schema = ProposePaymentInput
    read_only = False

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ProposePaymentInput)

        try:
            from_account = await _resolve_owned_account(
                self._supabase, context, validated_input.from_account_id
            )
        except IdentityError:
            logger.warning(
                "access denied user_id=%s tool=%s", context.user_id, self.name
            )
            raise

        insufficient_funds = await _insufficient_funds_error(
            self._supabase, self.name, from_account, validated_input.amount_minor
        )
        if insufficient_funds is not None:
            return insufficient_funds

        # THE GUARDRAIL: re-resolve the real account holder from the IBAN
        # here, server-side, rather than trusting `beneficiary_name` (model-
        # authored, ultimately traceable back to whatever the user typed in
        # conversation). The proposal - what the user actually sees and
        # confirms with Face ID/password - is built from this verified name,
        # never from the conversational one. See resolve_iban_holder.py for
        # the read-only tool that lets the model show this name and get
        # confirmation *before* calling this tool in the first place.
        from app.modules.accounts import service as accounts_service

        try:
            holder = await accounts_service.get_account_holder_by_iban(
                self._supabase, validated_input.to_iban
            )
        except IbanNotFoundError:
            return ToolResult.failure(
                name=self.name,
                error=(
                    "IBAN-ul nu aparține niciunui client BanK - nu se poate face "
                    "o plată către el."
                ),
            )
        real_beneficiary_name = f"{holder['first_name']} {holder['last_name']}"

        amount_str = _format_amount(validated_input.amount_minor, from_account["currency"])
        summary = f"Plată de {amount_str} către {real_beneficiary_name}"

        from app.modules.chat.proposals_service import create_proposal

        proposal = await create_proposal(
            self._supabase,
            user_id=context.user_id,
            conversation_id=_require_conversation(context, self.name),
            proposal_type="payment",
            payload={
                "from_account_id": validated_input.from_account_id,
                "to_iban": validated_input.to_iban,
                "beneficiary_name": real_beneficiary_name,
                "amount_minor": validated_input.amount_minor,
                "description": validated_input.description,
            },
            summary=summary,
        )
        return ToolResult(
            name=self.name,
            data={
                "proposal_id": proposal["id"],
                "summary": summary,
                "resolved_holder_name": real_beneficiary_name,
            },
        )


class ProposeOpenAccountInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    currency: str = Field(default="RON", min_length=3, max_length=3)
    product_type: Literal["checking", "savings", "term_deposit"] = "checking"
    term_months: int | None = Field(
        default=None, description="Required only when product_type is term_deposit."
    )

    @model_validator(mode="after")
    def _term_months_required_for_term_deposit(self) -> ProposeOpenAccountInput:
        if self.product_type == "term_deposit" and self.term_months is None:
            raise ValueError("term_months is required when product_type is term_deposit")
        return self


class ProposeOpenAccountTool(Tool):
    name = "propose_open_account"
    description = (
        "Pregătește o propunere de deschidere a unui cont nou (curent, "
        "economii sau depozit la termen). Contul NU se deschide - "
        "utilizatorul trebuie să confirme, cu Face ID sau parolă."
    )
    input_schema = ProposeOpenAccountInput
    read_only = False

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ProposeOpenAccountInput)

        label = _PRODUCT_LABELS_RO[validated_input.product_type]
        summary = (
            f"Deschidere cont {label} «{validated_input.name}» în {validated_input.currency}"
        )

        from app.modules.chat.proposals_service import create_proposal

        proposal = await create_proposal(
            self._supabase,
            user_id=context.user_id,
            conversation_id=_require_conversation(context, self.name),
            proposal_type="open_account",
            payload={
                "name": validated_input.name,
                "currency": validated_input.currency,
                "product_type": validated_input.product_type,
                "term_months": validated_input.term_months,
            },
            summary=summary,
        )
        return ToolResult(
            name=self.name, data={"proposal_id": proposal["id"], "summary": summary}
        )


class ProposeCloseAccountInput(BaseModel):
    account_id: str = Field(description="One of the user's own account identifiers.")


class ProposeCloseAccountTool(Tool):
    name = "propose_close_account"
    description = (
        "Pregătește o propunere de închidere a unui cont. Contul trebuie să "
        "aibă sold zero. Contul NU se închide - utilizatorul trebuie să "
        "confirme, cu Face ID sau parolă."
    )
    input_schema = ProposeCloseAccountInput
    read_only = False

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ProposeCloseAccountInput)

        try:
            account = await _resolve_owned_account(
                self._supabase, context, validated_input.account_id
            )
        except IdentityError:
            logger.warning(
                "access denied user_id=%s tool=%s", context.user_id, self.name
            )
            raise

        summary = f"Închidere cont «{account['name']}»"

        from app.modules.chat.proposals_service import create_proposal

        proposal = await create_proposal(
            self._supabase,
            user_id=context.user_id,
            conversation_id=_require_conversation(context, self.name),
            proposal_type="close_account",
            payload={"account_id": validated_input.account_id},
            summary=summary,
        )
        return ToolResult(
            name=self.name, data={"proposal_id": proposal["id"], "summary": summary}
        )


class ProposeCancelCardInput(BaseModel):
    card_id: str = Field(description="One of the user's own card identifiers.")


class ProposeCancelCardTool(Tool):
    name = "propose_cancel_card"
    description = (
        "Pregătește o propunere de ANULARE PERMANENTĂ a unui card. Cardul nu "
        "poate fi reactivat după anulare. Aceasta NU este o blocare "
        "temporară - nu există nicio unealtă pentru blocare temporară. "
        "Cardul NU se anulează prin acest apel - utilizatorul trebuie să "
        "confirme, cu Face ID sau parolă."
    )
    input_schema = ProposeCancelCardInput
    read_only = False

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ProposeCancelCardInput)

        card = await self._get_owned_card(context, validated_input.card_id)
        summary = f"Anulare permanentă card •••• {card['last4']}"

        from app.modules.chat.proposals_service import create_proposal

        proposal = await create_proposal(
            self._supabase,
            user_id=context.user_id,
            conversation_id=_require_conversation(context, self.name),
            proposal_type="cancel_card",
            payload={"card_id": validated_input.card_id},
            summary=summary,
        )
        return ToolResult(
            name=self.name, data={"proposal_id": proposal["id"], "summary": summary}
        )

    async def _get_owned_card(self, context: Context, card_id: str) -> dict:
        """Cards have no direct entry in `Context.account_ids` - ownership is
        the card's account being one the context user owns, same chain
        cards/service.py::cancel_card checks (card -> account -> user)."""
        resp = (
            await self._supabase.table("cards")
            .select("*")
            .eq("id", card_id)
            .maybe_single()
            .execute()
        )
        card = resp.data if resp is not None else None
        if card is None or not context.owns(str(card["account_id"])):
            logger.warning(
                "access denied user_id=%s tool=%s", context.user_id, self.name
            )
            raise AccessDeniedError()
        return card


def _require_conversation(context: Context, tool_name: str) -> str:
    """propose_* tools always run inside a real chat turn, which always has a
    conversation_id (see chat/router.py) - the only exception is the CLI's
    dev_context, which has none. Fails as a clean tool error rather than a
    NOT NULL constraint violation bubbling up as a raw 500."""
    if context.conversation_id is None:
        raise IdentityError(
            f"{tool_name} requires an active conversation, which this caller has none of"
        )
    return context.conversation_id

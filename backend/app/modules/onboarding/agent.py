"""Comodul - the calm, patient assistant that interviews a new user for the
fields /auth/register needs, then hands off to that endpoint's normal
validation for the actual account creation.

Same tool-calling loop shape as app/ai/agents/banking_agent.py::BankingAgent
- see that file for the detailed rationale of each step. Kept as a separate
class rather than sharing a base implementation: this agent's tool set (one
tool, pure, no side effects, no identity/account resolution) is different in
kind from the banking agent's, and Agent's base contract is already thin
enough that duplicating the loop is cheaper than generalizing it for two
callers.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.ai.agents.base import Agent
from app.ai.context import Context
from app.ai.language_directive import language_directive
from app.ai.providers.base import ModelProvider
from app.ai.schemas import Message, ToolCall, ToolResult
from app.ai.tools.registry import ToolRegistry
from app.ai.turn import TurnResult

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ești Comodul, asistentul calm și răbdător care ajută un \
client nou să își deschidă un cont la BanK.

Tonul tău: calm, prietenos, răbdător - niciodată grăbit sau formal-rece. \
Pui o singură întrebare pe rând (sau două strâns legate), ca într-o \
conversație reală, nu ca un formular completat mecanic.

ORDINEA CONTEAZĂ - citește cu atenție:

Pasul 1. Cere ÎNTÂI CNP-ul și adresa de email, înaintea oricărui altui \
câmp (nume, parolă etc). Poți menționa din start că poate încărca o poză \
a buletinului dacă e mai simplu - aplicația citește automat CNP-ul, \
numele și adresa din ea; tu doar aștepți rezultatul citirii într-un mesaj \
și confirmi politicos ce ai înțeles. Dar tot ai nevoie și de email (nu \
vine din poză) înainte de pasul 2.

Dacă mesajul cu rezultatul citirii pozei menționează că poza pare neclară \
(sau că nu s-a putut citi nimic din ea), NU trece mai departe cu datele \
citite ca și cum ar fi sigure - spune-i calm că poza nu a ieșit foarte \
clar și întreabă-l dacă vrea să încarce alta, mai clară, sau preferă să \
completeze acele date manual, prin chat.

Pasul 2. De îndată ce ai AMBELE (CNP și email), cheamă IMEDIAT tool-ul \
check_existing_account cu ele, înainte să mai întrebi orice altceva. Nu \
aștepta să aduni toate datele mai întâi - asta e ineficient și enervant \
pentru client dacă are deja un cont.

Pasul 3a. Dacă check_existing_account raportează exists=true: STOP. Nu mai \
continua interviul de înregistrare (nu mai ceri nume, parolă etc). Spune-i \
calm și direct că pare să aibă deja un cont și că aplicația îi arată o \
opțiune de resetare a parolei chiar acolo, în conversație.

Pasul 3b. Dacă exists=false, continuă interviul normal, cerând în ordine:
- Nume și prenume

Apoi întrebi, specificând clar că sunt OPȚIONALE și pot fi sărite:
- Telefon (opțional)
- Adresă (opțională)
- Cod de referral (opțional - dacă are unul, primește 500 RON cadou la \
deschiderea contului)

NU ceri NICIODATĂ parola prin chat, sub nicio formă - nici măcar dacă \
utilizatorul o oferă spontan. Parola nu are ce căuta într-o conversație cu \
un model de limbaj: ecranul de confirmare pe care aplicația îl arată la \
final are un câmp de parolă real, separat, prin care utilizatorul o \
introduce direct, fără să treacă vreodată prin chat. Dacă utilizatorul \
scrie o parolă în chat oricum, nu o repeta înapoi și nu o incluzi în \
tool-ul de mai jos - spune-i calm că o va introduce chiar pe ecranul de \
confirmare, din motive de siguranță.

Odată ce ai toate câmpurile obligatorii (fără parolă - vezi mai sus), ai \
deja verificat exists=false, și ai întrebat explicit despre fiecare câmp \
opțional (chiar dacă a fost sărit), cheamă tool-ul propose_registration cu \
tot ce ai adunat (null pentru câmpurile opționale sărite). NU inventa \
niciodată o valoare pe care utilizatorul nu a dat-o. După ce chemi tool-ul, \
rezumă natural datele adunate, menționează că mai are un singur pas - să \
își aleagă parola direct pe ecranul de confirmare - și întreabă dacă poate \
confirma crearea contului - aplicația se ocupă de restul.

Nu creezi tu contul - tool-ul propose_registration doar pregătește datele \
pentru ca aplicația să le arate clientului spre confirmare finală."""

#: Cap on provider round-trips per user message. Prevents an infinite tool loop.
MAX_ITERATIONS = 5

FALLBACK_REPLY = (
    "Îmi pare rău, am nevoie de puțin mai mult timp să procesez asta. "
    "Poți reformula, te rog?"
)


class OnboardingAgent(Agent):
    """Runs the propose_registration tool loop against a provider."""

    name = "onboarding"

    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        max_iterations: int = MAX_ITERATIONS,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self._provider = provider
        self._tools = tools
        self._system_prompt = system_prompt
        self._max_iterations = max_iterations

    async def run(self, messages: Sequence[Message], context: Context) -> TurnResult:
        """Same loop as ToolLoopAgent, minus handoff: onboarding runs outside
        the orchestrator entirely (no session, no Context identity yet), so
        there is no chain for it to be part of and `handoff` is always None."""
        working: list[Message] = [
            Message(
                role="system",
                content=self._system_prompt + language_directive(context.language),
            ),
            *messages,
        ]
        trace_start = len(working)
        specs = self._tools.list_specs()

        for iteration in range(1, self._max_iterations + 1):
            response = self._provider.complete(working, specs)

            if not response.wants_tools:
                return TurnResult(reply=response.text or "", trace=working[trace_start:])

            working.append(response.to_assistant_message())
            for call in response.tool_calls:
                logger.info(
                    "agent=%s iteration=%d executing tool=%s",
                    self.name,
                    iteration,
                    call.name,
                )
                result = await self._execute(call, context)
                working.append(result.to_message())

        logger.warning(
            "agent=%s hit max_iterations=%d; returning fallback",
            self.name,
            self._max_iterations,
        )
        return TurnResult(reply=FALLBACK_REPLY, trace=working[trace_start:])

    async def _execute(self, call: ToolCall, context: Context) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            logger.warning("agent=%s requested unknown tool=%s", self.name, call.name)
            return ToolResult.failure(
                name=call.name,
                error=f"unknown tool: {call.name}",
                tool_call_id=call.id,
            )
        return await tool.execute(call, context)

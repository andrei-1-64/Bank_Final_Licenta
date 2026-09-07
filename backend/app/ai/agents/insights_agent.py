"""The insights agent: analytical and observational, where banking is factual.

This is a genuinely different *kind* of reasoning, not just a different set of
tools — which is what makes it a separate agent rather than another tool group
on BankingAgent. BankingAgent answers "what is true right now" and is forbidden
from speculating; InsightsAgent is asked "what does this mean" and is expected
to interpret, compare periods, and point out patterns. One prompt cannot be
both strictly literal and usefully interpretive.

Still read-only: it may suggest what the user *could* do, never claim to have
done it. Write/propose tools arrive in Step 11.

Since Step 15 it also holds `handoff_to_agent`, which is the one thing here
that is not a read - but it does not make this agent able to act. All it does
is ask for the rest of the turn to continue on an agent that can (BankingAgent
for an action, PlanningAgent for a goal). Whether that is permitted at all is
decided in `Orchestrator.dispatch`, against a table this agent's model cannot
reach; see ALLOWED_HANDOFF_TARGETS.
"""

from __future__ import annotations

import logging

from app.ai.agents.currency_rules import CURRENCY_ROUTING_RULES
from app.ai.agents.scope_guardrail import OFF_TOPIC_GUARDRAIL
from app.ai.agents.tool_loop import ToolLoopAgent
from app.ai.routing import RoutingRule

logger = logging.getLogger(__name__)

#: Keyword STEMS matched as word prefixes on a diacritic-folded message (see
#: `RoutingRule.matched`). Split into several named rules so a persisted
#: `RoutingDecision.matched_rule` says WHICH kind of analytical intent fired,
#: not just "insights".
#:
#: OVERLAP WITH BANKING: `cheltui` and `bani` are also BankingAgent keywords
#: (added in Step 6, before this agent existed). `bani` stays a plain
#: single-stem match here (genuinely transactional-default - see item 5 of
#: Step 16 Priority 2). `cheltui` does not: since Step 16 Priority 2 item 7,
#: `insights_spending_analysis` below only claims it alongside one of
#: `INSIGHTS_ANALYTICAL_MARKERS` - a plain statement of fact ("am cheltuit 50
#: lei pe cafea") is not itself analytical, and correctly falls through
#: (registration order still puts this agent before Banking, so the two only
#: differ when a bare stem match would otherwise have been ambiguous) to
#: BankingAgent's own unconditional `cheltui`, exactly as it did before this
#: rule existed. See `AIService.__init__` for the registration order.
#:
#: `anul` rather than `an`: prefix matching means `an` would claim every word
#: starting with those two letters — including names like "Andrei", which would
#: send "trimite 50 RON către Andrei" to the analytics agent. `anul`/`anual`
#: cover "anul acesta" / "anul trecut" without that blast radius.
#:
#: `"anul "` (trailing space) rather than bare `"anul"`: prefix matching also
#: means `anul` claims "anulează"/"anulare"/"anulat" (cancel-family words,
#: e.g. propose_cancel_card's "anulează cardul meu" - see
#: app/ai/tools/propose_tools.py), sending a card-cancellation request here
#: instead of to Banking. The trailing space still matches "anul acesta" /
#: "anul trecut" (a space always follows in that phrasing) while rejecting
#: "anulează", which continues the same word with no boundary. Narrow
#: regression: no longer matches "anul" as the very last word of a message,
#: or followed by punctuation instead of a space - accepted, since the
#: false-positive it fixes actively broke a real feature.
#: Analytical/comparative/temporal markers that turn a bare "cheltui" mention
#: into an analytical question rather than a plain statement of fact (Step 16
#: Priority 2, item 7). One list covers both cases the task describes -
#: "unde"/"cati"/"cat"-style analytical questions and "ultima luna"/
#: "ultimele"-style comparative/temporal ones - since a single marker match
#: is enough either way.
INSIGHTS_ANALYTICAL_MARKERS = frozenset(
    {
        "unde",
        "cati",
        "cate",
        "cat",
        "in ce",
        "cum",
        "cel mai",
        "top",
        "categori",
        "ultima luna",
        "ultimele",
    }
)

INSIGHTS_ROUTING_RULES = (
    # FIRST, ahead of `insights_time_slice`'s `luna`: "cursul euro luna asta"
    # is a rate question, not a spending analysis. Registered on BankingAgent
    # too - see currency_rules.py for why both, and for the collision check.
    *CURRENCY_ROUTING_RULES,
    RoutingRule(
        name="insights_spending",
        keywords=frozenset({"spend", "expense"}),
    ),
    RoutingRule(
        name="insights_spending_analysis",
        keywords=frozenset({"cheltui"}),
        requires_any_of=INSIGHTS_ANALYTICAL_MARKERS,
    ),
    RoutingRule(
        name="insights_analysis",
        keywords=frozenset({"analiz", "tendin", "trend", "patter", "unde"}),
    ),
    RoutingRule(
        name="insights_categories",
        keywords=frozenset(
            {"categori", "abonament", "subscription", "recurent", "recurring"}
        ),
    ),
    RoutingRule(
        name="insights_time_slice",
        keywords=frozenset(
            {
                "saptaman",
                "week",
                "luna",
                "lunar",
                "month",
                "trimestr",
                "quarter",
                "anul ",
                "anual",
            }
        ),
    ),
)

SYSTEM_PROMPT = f"""{OFF_TOPIC_GUARDRAIL}

Ești asistentul analitic al băncii. Rolul tău este să ajuți
utilizatorul să înțeleagă cum își cheltuie și își gestionează banii — analize,
categorii, tendințe, tipare de cheltuieli.

REGULI:
- Ai voie să interpretezi datele și să oferi observații („pare că cheltuielile
  tale pe mâncare s-au dublat comparativ cu luna trecută").
- NU inventa cifre — folosește doar datele returnate de instrumente.
- NU poți efectua acțiuni: nu poți muta bani, seta limite, bloca carduri, sau
  modifica conturi. Dacă utilizatorul cere așa ceva, spune clar că poți doar să
  analizezi, nu să acționezi.
- Poți sugera ce ar putea utilizatorul să facă („ai putea să-ți setezi o limită
  pe categoria X"), dar niciodată să nu pretinzi că ai făcut-o.
- Convertește sumele din unități minore în format lizibil (RON cu virgulă
  zecimală, două zecimale: 50000 înseamnă „500,00 RON").
- Pentru conversii valutare folosește convert_currency, niciodată un curs din
  memoria ta. `amount` este suma în unități ÎNTREGI ale monedei (100 pentru
  100 EUR, nu 10000) — altfel decât la celelalte unelte. Spune mereu data
  cursului BNR (`rate_date`); dacă `stale` este adevărat, menționează că este
  ultimul curs cunoscut. Dacă unealta dă eroare, spune că nu ai cursul.
- Datele: pentru interogări cu intervale („săptămâna trecută", „luna
  octombrie", „ultimele 3 luni"), calculează tu datele ISO (start_date,
  end_date) și pasează-le instrumentului. Data de azi este ziua curentă.
  Dacă utilizatorul NU specifică nicio perioadă, folosește implicit
  ultimele 30 de zile, fără să întrebi - menționează pe scurt perioada
  aleasă în răspuns. Întreabă o singură dată despre perioadă doar când
  mesajul se referă la un interval neclar pe care 30 de zile nu îl
  aproximează rezonabil (de ex. „compară cu perioada anterioară" fără să
  reiasă din context care e perioada de bază).
- `direction` este „debit" (bani ieșiți) sau „credit" (bani intrați).
  Cheltuielile sunt tranzacțiile de tip debit.
- Dacă instrumentul întoarce o listă goală, spune clar că nu există activitate
  în intervalul cerut — nu este o eroare.
- Formatează răspunsurile în română, concis dar cu observații utile — nu doar
  tabele.
- Fii scurt: 3-5 propoziții, nu un eseu. Spune concluzia/cifra direct, apoi
  cel mult o observație utilă.
- Pune CEL MULT o întrebare de clarificare într-un mesaj, și doar când chiar
  blochează analiza (vezi mai sus, la perioade). Nu înșira mai multe întrebări
  sau opțiuni în același mesaj - dacă utilizatorul vrea detalii sau
  alternative, le poate cere separat.

INSTRUMENTE DISPONIBILE:
- get_transactions_in_range: preia tranzacțiile utilizatorului dintr-un interval
  de date, pe toate conturile. Folosește pentru analize, categorii, tendințe.
- categorize_transactions: împarte cheltuielile într-un interval pe categorii
  (mâncare, abonamente, transport etc.). Folosește pentru „ce am cheltuit pe
  mâncare?" sau „categorii de cheltuieli".
- detect_recurring_payments: găsește plățile recurente / abonamentele din
  istoricul tranzacțiilor. Folosește pentru „am abonamente recurente?", „ce
  abonamente am?" sau „cât plătesc pe subscripții?".
- compute_spending_stats: calculează statistici agregate (venituri, cheltuieli,
  net, medii, cea mai mare/mică tranzacție, ziua cu cele mai multe cheltuieli)
  pentru un interval. Folosește pentru „cât am cheltuit luna asta?",
  „statistici" sau „rezumat financiar".
- detect_anomalies: semnalează tranzacții neobișnuite (sumă mult peste normal
  sau comerciant niciodată văzut până acum). Folosește pentru „am cheltuieli
  neobișnuite?", „ceva suspect?" sau „tranzacții ciudate".
- compare_statement_to_ledger: compară rândurile extrase din extrasul de
  cont activ al conversației cu tranzacțiile reale din jurnalul contabil,
  pentru aceeași perioadă. Folosește pentru „se potrivește extrasul cu
  contul meu?" sau „verifică extrasul". Nu are argumente - dacă nu există
  niciun extras activ, instrumentul întoarce o eroare clară; spune-i
  utilizatorului să încarce un extras mai întâi.

NOTĂ despre extrase de cont: când conversația are un extras de cont activ,
toate instrumentele de mai sus (cu excepția compare_statement_to_ledger)
citesc automat din rândurile extrase din acel extras, nu din jurnalul
contabil real - acele rânduri sunt EXTRASE AUTOMAT și NEVERIFICATE, pot
conține erori de citire. Menționează asta dacă utilizatorul pare să creadă
că analizezi jurnalul contabil real.

- handoff_to_agent: predă restul acestei conversații altui agent, care
  continuă în ACEEAȘI tură și îi răspunde utilizatorului. Tu tot nu poți
  acționa - dar poți duce constatarea ta la cineva care poate.

Poți combina instrumente în aceeași conversație când întrebarea o cere - de
exemplu „rezumatul lunii" poate însemna atât compute_spending_stats cât și
categorize_transactions.

CÂND SĂ PREDAI CONVERSAȚIA (handoff_to_agent):
- Dacă detect_recurring_payments găsește o plată recurentă / un abonament pe
  care utilizatorul pare să vrea să îl OPREASCĂ („nu mai vreau abonamentul
  X", „cum scap de plata asta lunară", „anulează-mi cardul pe care se ia
  X"), cheamă handoff_to_agent cu target_agent="banking". Agentul bancar
  poate pregăti o propunere de anulare a cardului; tu nu poți.
- Dacă utilizatorul vrea, pornind de la analiza ta, un PLAN sau o proiecție
  („cât aș economisi în 6 luni dacă renunț la abonamentele astea?"), cheamă
  handoff_to_agent cu target_agent="planning".
- ÎNTREBARE COMPUSĂ - două cereri într-un singur mesaj, din care tu poți
  acoperi doar una (de ex. „care e soldul meu și cât am cheltuit luna asta?":
  cheltuielile sunt ale tale, soldul nu). Atunci:
  1. Scrie ÎNTÂI răspunsul tău complet la partea ta, ca text normal.
  2. Abia apoi cheamă handoff_to_agent cu target_agent="banking" și
     context_hint = exact partea rămasă, formulată ca cerere a
     utilizatorului („Utilizatorul vrea și soldul conturilor sale.").
  Ordinea contează: dacă chemi unealta înainte să scrii, partea ta de
  răspuns se pierde.
  Predă astfel DOAR pentru părți pe care celălalt agent le CITEȘTE: sold,
  conturi, carduri, tranzacții, transferuri. Dacă partea rămasă cere o
  ACȚIUNE (transfer, plată, blocare sau anulare de card, deschidere sau
  închidere de cont), NU preda automat pentru ea - răspunde la partea ta și
  spune-i utilizatorului să ceară acțiunea separat, ca să treacă prin
  confirmările ei normale.
- Dacă rămân trei sau mai multe cereri distincte, nu încerca să le acoperi pe
  toate într-o tură: răspunde la partea ta, predă cel mult o dată, iar pentru
  ce rămâne încheie cu o singură propoziție de forma „Pentru X, te rog
  întreabă-mă separat." Nicio parte a întrebării nu are voie să rămână fără
  niciun răspuns și fără nicio mențiune.
- Nu preda pentru orice sugestie. Doar când utilizatorul chiar vrea să se
  întâmple ceva, iar tu nu ai instrumentul potrivit. Dacă nu e clar ce vrea,
  întreabă-l întâi, normal, în conversație.
- Cum completezi argumentele:
  * target_agent: „banking" sau „planning". Nimic altceva.
  * reason: pe scurt, de ce e nevoie de celălalt agent („plată recurentă pe
    care utilizatorul vrea să o oprească").
  * context_hint: instrucțiunea concretă pentru celălalt agent, ca și cum ar
    fi mesajul utilizatorului. Numește exact despre ce e vorba - comerciantul,
    suma și cardul, dacă le știi. De exemplu: „Utilizatorul vrea să scape de
    abonamentul GymPass, 12000 în unități minore pe lună, plătit pe cardul
    care se termină în 4321. Propune anularea acelui card."
- După ce ai chemat handoff_to_agent, NU mai scrie nimic: celălalt agent
  este cel care îi răspunde utilizatorului de acum înainte.
- Predarea nu este garantată. Poate fi refuzată (de exemplu când conversația
  are un extras de cont activ). Dacă vrei să fii sigur că utilizatorul
  primește ceva util, spune-ți concluzia analitică ÎNAINTE de a preda.
"""

FALLBACK_REPLY = (
    "Nu am reușit să duc analiza la capăt — am tot avut nevoie de date "
    "suplimentare și m-am oprit ca să nu intru în buclă. Încearcă să "
    "reformulezi întrebarea."
)


class InsightsAgent(ToolLoopAgent):
    """Analytical agent over the user's transaction history."""

    name = "insights"
    routing_rules = INSIGHTS_ROUTING_RULES
    system_prompt = SYSTEM_PROMPT
    fallback_reply = FALLBACK_REPLY

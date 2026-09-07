"""The planning agent: goal-oriented and forward-looking, where Banking is
factual ("what IS") and Insights is retrospective ("what WAS/DID").

Planning is asked "what happens next, and can I get where I want to be" - a
third kind of reasoning again, not a third tool group bolted onto an
existing agent. Still read-only: it proposes and projects, it never acts.
Propose/confirm (write tools with user approval) arrive in Step 11.
"""

from __future__ import annotations

import logging

from app.ai.agents.scope_guardrail import OFF_TOPIC_GUARDRAIL
from app.ai.agents.tool_loop import ToolLoopAgent
from app.ai.routing import RoutingRule

logger = logging.getLogger(__name__)

#: Keyword STEMS matched as word prefixes on a diacritic-folded message (see
#: `RoutingRule.matched`). Split into several named rules so a persisted
#: `RoutingDecision.matched_rule` says WHICH kind of planning intent fired.
#:
#: COLLISION, RESOLVED (Step 16 Priority 2, item 7): `econom` is also a
#: BankingAgent keyword (its "Economii" account), and Banking is registered
#: first - a bare stem match would always reach Banking before Planning is
#: even checked. `planning_savings_goal` below and BankingAgent's own
#: `banking_savings_default` rule resolve this together: Planning claims
#: "econom" only alongside one of `PLANNING_FORWARD_MARKERS` (a forward-
#: looking, goal-setting phrasing); Banking explicitly backs off "econom"
#: in exactly that same case, via `excludes_any_of`, so its otherwise-first
#: registration no longer shadows Planning. A bare "econom" with no such
#: marker (e.g. past tense - "am economisit 500 lei") still isn't specific
#: enough to be a goal, and correctly falls through to Banking, unchanged
#: from before this split.
PLANNING_FORWARD_MARKERS = frozenset(
    {
        "vreau",
        "ar trebui",
        "as vrea",
        "plan",
        "planific",
        "viitor",
        "obiectiv",
        "tinta",
        "luna viitoare",
        "anul viitor",
    }
)

PLANNING_ROUTING_RULES = (
    RoutingRule(
        name="planning_goals",
        keywords=frozenset({"obiectiv", "goal", "sav", "strangi", "adun"}),
    ),
    RoutingRule(
        name="planning_savings_goal",
        keywords=frozenset({"econom"}),
        requires_any_of=PLANNING_FORWARD_MARKERS,
    ),
    RoutingRule(
        name="planning_projection",
        keywords=frozenset({"proiect", "proiecti", "predict", "forecast", "viitor", "futur"}),
    ),
    RoutingRule(
        name="planning_scenario",
        keywords=frozenset({"daca", "what if", "simul", "scenari", "presupun"}),
    ),
    RoutingRule(
        name="planning_timeline",
        keywords=frozenset({"cand", "when", "pana", "by when", "termen"}),
    ),
    RoutingRule(
        name="planning_budget",
        keywords=frozenset({"buget", "budget", "plan", "strategi"}),
    ),
)

SYSTEM_PROMPT = f"""{OFF_TOPIC_GUARDRAIL}

Ești planificatorul financiar al băncii. Rolul tău este să
ajuți utilizatorul să planifice, să proiecteze și să simuleze scenarii
financiare viitoare.

REGULI:
- Gândește orientat pe obiective: utilizatorul are un scop („vreau să-mi iau
  un PS5", „vreau să economisesc pentru vacanță") — ajută-l să ajungă acolo.
- Folosește datele reale ale utilizatorului (sold curent, venituri/cheltuieli
  recente) pentru proiecții realiste.
- Poți sugera acțiuni concrete („ar trebui să economisești 285 RON pe lună"),
  dar NU poți executa nimic — nu muti bani, nu creezi transferuri, nu setezi
  automatizări.
- Dacă un obiectiv nu este realizabil în termenul dat, spune-o direct și
  oferă alternative (termen mai lung, sumă mai mică, reducere cheltuieli).
- Convertește sumele din unități minore în format lizibil (RON cu virgulă
  zecimală, două zecimale: 50000 înseamnă „500,00 RON").
- Toate datele și răspunsurile în română.
- Fii scurt: 3-5 propoziții, nu un eseu. Dă cifra/concluzia direct (suma
  lunară necesară, dacă obiectivul e realizabil sau nu), fără să explici pas
  cu pas cum ai calculat sau să repeți datele de intrare pe care utilizatorul
  le știe deja. O singură recomandare concretă, nu o listă lungă de opțiuni -
  dacă utilizatorul vrea detalii sau alternative, le poate cere.
- Pune CEL MULT o întrebare de clarificare într-un mesaj, și doar când chiar
  blochează calculul (de ex. nici suma țintă, nici termenul nu reies din
  mesaj sau din conversație). Dacă lipsește un singur element și restul e
  clar, alege un implicit rezonabil pe baza datelor reale ale utilizatorului
  și spune ce ai presupus, în loc să întrebi. Nu înșira mai multe întrebări
  în același mesaj.

INSTRUMENTE DISPONIBILE:
- project_balance: proiectează soldul pe N luni, bazat pe istoricul real sau
  pe un ritm de economisire specificat.
- simulate_scenario: „ce-ar fi dacă?" — aplică ajustări (reducere cheltuieli,
  mărire venituri) și compară cu scenariul de bază.
- savings_goal: verifică dacă un obiectiv financiar este realizabil într-un
  termen dat și cât trebuie economisit lunar.
- handoff_to_agent: predă restul acestei conversații altui agent, care
  continuă în ACEEAȘI tură și îi răspunde utilizatorului. Tu tot nu poți
  acționa - dar poți duce planul tău la cineva care poate.

ÎNTREBARE COMPUSĂ (handoff_to_agent):
- Dacă mesajul conține două cereri, iar tu poți acoperi doar una (de ex.
  „cât ar trebui să economisesc lunar și care e soldul meu acum?"):
  1. Scrie ÎNTÂI răspunsul tău complet la partea de planificare, ca text.
  2. Abia apoi cheamă handoff_to_agent cu target_agent="banking" și
     context_hint = exact partea rămasă, formulată ca cerere a
     utilizatorului („Utilizatorul vrea și soldul conturilor sale.").
  Dacă chemi unealta înainte să scrii, partea ta de răspuns se pierde.
- Predă astfel DOAR pentru părți pe care celălalt agent le CITEȘTE: sold,
  conturi, carduri, tranzacții, transferuri. Dacă partea rămasă cere o
  ACȚIUNE (transfer, plată, blocare sau anulare de card, deschidere sau
  închidere de cont), NU preda automat pentru ea - răspunde la partea ta și
  spune-i utilizatorului să ceară acțiunea separat, ca să treacă prin
  confirmările ei normale.
- Pentru orice rămâne neacoperit după o singură predare, încheie cu o
  propoziție de forma „Pentru X, te rog întreabă-mă separat." Nicio parte a
  întrebării nu are voie să rămână fără niciun răspuns și fără nicio mențiune.
- După ce ai chemat handoff_to_agent, NU mai scrie nimic: celălalt agent este
  cel care îi răspunde utilizatorului de acum înainte. Predarea nu este
  garantată și poate fi refuzată.
"""

FALLBACK_REPLY = (
    "Nu am reușit să duc planificarea la capăt — am tot avut nevoie de date "
    "suplimentare și m-am oprit ca să nu intru în buclă. Încearcă să "
    "reformulezi întrebarea."
)


class PlanningAgent(ToolLoopAgent):
    """Goal-oriented agent that projects and simulates the user's finances."""

    name = "planning"
    routing_rules = PLANNING_ROUTING_RULES
    system_prompt = SYSTEM_PROMPT
    fallback_reply = FALLBACK_REPLY

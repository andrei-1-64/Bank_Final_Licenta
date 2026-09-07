"""Command-line REPL against the real Azure provider.

    python -m app.ai.chat

Type 'exit' (or Ctrl-C / Ctrl-D) to quit.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.ai.context import dev_context
from app.ai.providers.base import ProviderError
from app.ai.schemas import Message
from app.ai.service import AIService
from app.config import ConfigurationError
from app.db.supabase_client import get_client

_EXIT_WORDS = {"exit", "quit", ":q"}

BANNER = """Banking assistant (read-only: balances come from the real ledger).
Signed in as {user_id} (DEV identity — real auth is not wired up yet).
Type 'exit' to quit.
"""


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    try:
        service = AIService(await get_client())  # real Azure provider
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # e.g. a malformed endpoint the SDK rejects outright
        print(f"Could not start the AI service: {exc}", file=sys.stderr)
        return 2

    # THE EDGE. Identity is established here, once, and threaded into every
    # turn — the model never supplies it. The CLI has no session cookie, so it
    # uses the dev identity; a real HTTP request builds its Context with
    # `build_context_for_user(user, supabase)` instead. Nothing else in the AI
    # layer changes between the two.
    context = dev_context()

    print(BANNER.format(user_id=context.user_id))
    history: list[Message] = []

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user_input:
            continue
        if user_input.lower() in _EXIT_WORDS:
            return 0

        try:
            turn = await service.handle_message(history, user_input, context)
            # One turn may now be several agent hops (see app.ai.turn); the CLI
            # only shows the final reply, but persists every hop's messages so
            # the next turn's history is what the models actually saw.
            reply = turn.final_reply
            history = [
                *history,
                Message(role="user", content=user_input),
                *turn.new_messages,
            ]
        except ProviderError as exc:
            # Keep the REPL alive; the history is unchanged so the user can retry.
            print(f"\nmodel error: {exc}\n", file=sys.stderr)
            continue
        except KeyboardInterrupt:
            print()
            return 0

        print(f"\nbot> {reply}\n")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

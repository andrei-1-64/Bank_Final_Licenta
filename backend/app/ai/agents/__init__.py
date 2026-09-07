"""Agents. One real agent for now; more are added, not swapped in."""

from app.ai.agents.banking_agent import BankingAgent
from app.ai.agents.base import Agent

__all__ = ["Agent", "BankingAgent"]

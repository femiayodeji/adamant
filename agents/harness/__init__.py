"""Evaluation harness: run the reference scaffold against many models.

Deliberately a separate artifact from ``agents/reference_agent.py``. The
published example is direct-Anthropic and optimised for being copied; the
harness is LiteLLM/OpenRouter so one client speaks to every provider. They share
the prompt and the decision loop (``agents/common``); only the client differs.
"""

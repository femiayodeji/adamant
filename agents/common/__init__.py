"""Shared pieces of an ADAM Arena agent.

The published reference agent (``agents/reference_agent.py``) and the evaluation
harness (``agents/harness/``) import everything here.  Only the model client
differs between them: the reference agent talks to the Anthropic SDK directly,
the harness talks to every vendor through LiteLLM/OpenRouter.
"""

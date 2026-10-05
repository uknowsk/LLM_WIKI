"""Personal (single-user, local PC) mode of the LLM wiki. Start with `python -m llmwiki.personal`.

This package is NOT a sync of the central wiki: it runs its own wiki over documents already on this PC.
The central server (`python -m llmwiki.web`) refuses the personal auth provider and WIKI_MODE=personal.
"""

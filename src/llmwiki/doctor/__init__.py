"""Self-diagnosis: `python -m llmwiki.doctor [--json] [--probe-context] [--skip-llm] [--skip-embed]`.

Prints a Korean PASS/WARN/FAIL checklist with remediation hints; exit code 1 if any check FAILs.
Output never contains secrets (every result is scrubbed against the configured keys) or document contents
(only built-in synthetic strings are ever parsed or sent).
"""

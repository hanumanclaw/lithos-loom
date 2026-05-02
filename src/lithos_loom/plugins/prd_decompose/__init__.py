"""prd-decompose plugin (US-12, US-13).

Reads a Pocock-shaped PRD doc from Lithos, runs Claude with a structured-output
prompt (template in ``prompt.md``, adapted from Pocock's ``to-issues`` skill),
writes one Lithos story doc per story, creates the per-PRD integration branch,
creates one Lithos task per story chained via ``metadata.depends_on``.
"""

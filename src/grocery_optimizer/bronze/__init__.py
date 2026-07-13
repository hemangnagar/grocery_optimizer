"""Bronze layer — raw fetchers (Kroger API, Lidl ESI, KCL aggregator, WFM).

Fetchers are added in later build steps; each writes raw bytes to
``data/bronze/`` and records a row in ``bronze_manifest``.
"""

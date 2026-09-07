"""Ingestion + retrieval for the docs agent's knowledge base.

Ingestion (pdf_export -> document_intelligence -> chunking -> ingest) only
ever runs offline, via `python -m scripts.ingest_knowledge_base`. `service.py`
is the only module this package exposes to the request path - it just reads
what ingestion already wrote.
"""

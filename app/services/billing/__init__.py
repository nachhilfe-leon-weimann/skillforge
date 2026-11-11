"""Billing services: lessons and their life cycle (billing spec).

Import the modules as namespaces (``from app.services.billing import lessons as lessons_service``).
The package imports only ``app.core``, ``app.integrations``, the CRM services and itself (billing spec, standing
criteria); the CRM imports nothing of it (ADR 0009).
"""

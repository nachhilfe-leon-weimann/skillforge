"""SkillForge's own background jobs (calendar-categories spec, ADR 0011): the queue in ``queue.py``.

Import the modules as namespaces (``from app.services.jobs import queue``). The package knows no domain: it imports
only ``app.core`` and itself, and a job's kind is a string to it.
"""

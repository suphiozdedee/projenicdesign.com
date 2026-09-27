from __future__ import annotations

TEMPLATE_VERSION = 5

DEFAULT_PROJECT_MEMORY = """# CCB Project Memory

This project uses CCB for visible multi-agent collaboration.

## Collaboration

- You are one agent in a CCB-managed project team.
- Use CCB `ask` for project-level collaboration with configured agents.
- Delegate with the goal, scope/files, assumptions, expected output, and verification needs.
- Reply concisely with findings, changes, verification, blockers, and risks when relevant.
"""

__all__ = ['DEFAULT_PROJECT_MEMORY', 'TEMPLATE_VERSION']

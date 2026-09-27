from __future__ import annotations

from dataclasses import replace
import re

from .policy import FILTER_CCB_INSTALL_BLOCKS, SOURCE_PROVIDER_USER_MEMORY
from .types import ProjectMemorySource

_MARKER_PAIRS = (
    ('<!-- CCB_CONFIG_START -->', '<!-- CCB_CONFIG_END -->'),
    ('<!-- CCB_ROLES_START -->', '<!-- CCB_ROLES_END -->'),
    ('<!-- REVIEW_RUBRICS_START -->', '<!-- REVIEW_RUBRICS_END -->'),
    ('<!-- CODEX_REVIEW_START -->', '<!-- CODEX_REVIEW_END -->'),
    ('<!-- GEMINI_INSPIRATION_START -->', '<!-- GEMINI_INSPIRATION_END -->'),
)

_LEGACY_SECTION_PATTERNS = (
    r'## Codex Collaboration Rules.*?(?=\n## (?!Gemini)|\Z)',
    r'## Codex 协作规则.*?(?=\n## |\Z)',
    r'## Gemini Collaboration Rules.*?(?=\n## |\Z)',
    r'## Gemini 协作规则.*?(?=\n## |\Z)',
    r'## OpenCode Collaboration Rules.*?(?=\n## |\Z)',
    r'## OpenCode 协作规则.*?(?=\n## |\Z)',
)


def filter_memory_source(
    source: ProjectMemorySource,
    *,
    filter_names: tuple[str, ...],
) -> ProjectMemorySource:
    if source.kind != SOURCE_PROVIDER_USER_MEMORY or not filter_names or not source.content:
        return source

    content = source.content
    applied: list[str] = []
    if FILTER_CCB_INSTALL_BLOCKS in filter_names:
        content, changed = _strip_ccb_install_blocks(content)
        if changed:
            applied.append(FILTER_CCB_INSTALL_BLOCKS)

    if not applied:
        return source
    return replace(
        source,
        content=_tidy_filtered_content(content),
        filtered=True,
        filter_names=tuple(applied),
    )


def _strip_ccb_install_blocks(content: str) -> tuple[str, bool]:
    result = content
    total = 0
    for start, end in _MARKER_PAIRS:
        escaped_start = re.escape(start)
        escaped_end = re.escape(end)
        line_block_pattern = (
            rf'^[^\S\n]*{escaped_start}[^\S\n]*(?:\r?\n)'
            rf'.*?'
            rf'^[^\S\n]*{escaped_end}[^\S\n]*(?:\r?\n)?'
        )
        result, count = re.subn(line_block_pattern, '', result, flags=re.DOTALL | re.MULTILINE)
        total += count
        inline_pattern = escaped_start + r'.*?' + escaped_end + r'(?:\r?\n)?'
        result, count = re.subn(inline_pattern, '', result, flags=re.DOTALL)
        total += count
    for pattern in _LEGACY_SECTION_PATTERNS:
        result, count = re.subn(pattern, '', result, flags=re.DOTALL)
        total += count
    return result, total > 0


def _tidy_filtered_content(content: str) -> str:
    stripped = content.strip()
    return f'{stripped}\n' if stripped else ''


__all__ = ['filter_memory_source']

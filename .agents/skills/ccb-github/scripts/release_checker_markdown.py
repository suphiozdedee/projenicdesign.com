from __future__ import annotations

import re


def markdown_section(body: str, heading: str) -> str | None:
    pattern = re.compile(
        rf"(?ms)^##\s+{re.escape(heading)}[^\n]*\n(?P<body>.*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(body)
    if not match:
        return None
    return match.group("body").strip()


def readme_release_block(body: str, version: str) -> str | None:
    pattern = re.compile(
        rf"(?ms)<summary><b>{re.escape(version)}</b>.*?</summary>(?P<body>.*?)(?=</details>)"
    )
    match = pattern.search(body)
    if not match:
        return None
    return match.group("body").strip()


def has_substantive_release_text(text: str | None) -> bool:
    if not text:
        return False
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("<!--", "-->", "<details", "</details>", "<summary", "</summary>")):
            continue
        cleaned_lines.append(stripped)
    return any(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", line) for line in cleaned_lines)


def semver_tuple(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(version or "").strip())
    if not match:
        return (-1, -1, -1)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def release_note_versions(body: str) -> list[str]:
    return re.findall(r"<summary><b>(v\d+\.\d+\.\d+)</b>", body)


def install_section(body: str, heading: str) -> str:
    pattern = re.compile(rf"(?ms)^##\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s+|\Z)")
    match = pattern.search(body)
    return match.group("body") if match else body

from __future__ import annotations

import sys


def require_supported_python(
    *,
    component: str,
    minimum: tuple[int, int] = (3, 10),
    version_info: tuple[int, int, int] | None = None,
) -> None:
    current = version_info or (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    if current[:2] >= minimum:
        return

    minimum_text = ".".join(str(part) for part in minimum)
    current_text = ".".join(str(part) for part in current[:3])
    raise RuntimeError(
        f"{component} requires Python {minimum_text}+; current interpreter is Python {current_text}. "
        "Use a supported interpreter that matches pyproject.toml."
    )

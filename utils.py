"""
dealerly/utils.py
=================
Pure helper utilities used across the pipeline.

No I/O except:
  - load_dotenv  (reads a file)
  - prompt_*     (stdin — only called from cli.py)

No imports from other Dealerly modules.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Date / time
# ---------------------------------------------------------------------------

def now_utc_iso() -> str:
    """Return current UTC time as a compact ISO-8601 string (no microseconds)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Environment / dotenv
# ---------------------------------------------------------------------------

def console_safe(s: str) -> str:
    """
    Make a string printable on legacy Windows consoles (cp1252) without
    UnicodeEncodeError. Replaces unencodable characters.
    """
    if not s:
        return s
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return s.encode(enc, errors="replace").decode(enc, errors="replace")
    except (LookupError, UnicodeError):
        return s.encode("ascii", errors="replace").decode("ascii")


def load_dotenv(path: Path, override: bool = True) -> None:
    """
    Load KEY=VALUE pairs from a .env file into os.environ.
    Skips blank lines and comments. Strips surrounding quotes from values.
    Does not require the python-dotenv package.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and (override or k not in os.environ):
            os.environ[k] = v


# ---------------------------------------------------------------------------
# Math / statistics
# ---------------------------------------------------------------------------

def clamp(x: float, lo: float, hi: float) -> float:
    """Clamp x to the closed interval [lo, hi]."""
    return max(lo, min(hi, x))


def median(xs: List[float]) -> Optional[float]:
    """
    Return the median of a list of floats, ignoring None values.
    Returns None if the list is empty after filtering.
    """
    values = sorted(float(x) for x in xs if x is not None)
    n = len(values)
    if not n:
        return None
    mid = n // 2
    return values[mid] if n % 2 == 1 else 0.5 * (values[mid - 1] + values[mid])


def round_to_nearest(value: float, step: int) -> int:
    """Round value to the nearest multiple of step (e.g. nearest £50)."""
    return int(round(value / step) * step)


# ---------------------------------------------------------------------------
# CLI prompts (only called from cli.py — kept here to avoid duplication)
# ---------------------------------------------------------------------------

def prompt_float(msg: str, default: float) -> float:
    """Prompt for a float, returning default on empty input."""
    s = input(f"{msg} [default {default}]: ").strip()
    return float(s) if s else float(default)


def prompt_int(msg: str, default: int) -> int:
    """Prompt for an int, returning default on empty input."""
    s = input(f"{msg} [default {default}]: ").strip()
    return int(s) if s else int(default)


def prompt_choice(msg: str, choices: Dict[str, str], default_key: str) -> str:
    """
    Prompt the user to pick from a labelled dict of choices.
    Example: prompt_choice("MOT mode", {"0":"off","1":"mock","2":"DVSA"}, "2")
    """
    label = "  ".join(f"({k}) {v}" for k, v in choices.items())
    return input(f"{msg}: {label} [default {default_key}]: ").strip() or default_key

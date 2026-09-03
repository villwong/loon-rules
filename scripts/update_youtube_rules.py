#!/usr/bin/env python3
"""Download, merge, and write the YouTube rules used by Loon."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path


V2FLY_URL = (
    "https://raw.githubusercontent.com/v2fly/domain-list-community/"
    "master/data/youtube"
)
BLACKMATRIX_URL = (
    "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/"
    "master/rule/Loon/YouTube/YouTube.list"
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPOSITORY_ROOT / "rule" / "YouTube.list"

SUPPORTED_LOON_TYPES = {
    "DOMAIN",
    "DOMAIN-SUFFIX",
    "DOMAIN-KEYWORD",
    "USER-AGENT",
    "IP-CIDR",
    "IP-CIDR6",
}
DOMAIN_TYPES = {"DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD"}
V2FLY_TYPE_MAP = {
    "domain": "DOMAIN-SUFFIX",
    "full": "DOMAIN",
    "keyword": "DOMAIN-KEYWORD",
}

# A successful HTTP response containing a truncated or error-like payload must not
# replace a healthy output file. Both current upstream lists are well above 100
# entries, so this leaves ample headroom while catching accidental truncation.
MIN_V2FLY_RULES = 100
MIN_BLACKMATRIX_RULES = 100

HEADER_TITLE = "# 自动合并自 v2fly + Blackmatrix7"
UPDATED_PREFIX = "# 自动更新时间: "
TOTAL_PREFIX = "# 总规则数: "


class UpdateError(RuntimeError):
    """Raised when an upstream cannot be safely used to update the output."""


def download_text(url: str, attempts: int = 3, timeout: int = 30) -> str:
    """Download UTF-8 text with bounded retries."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "loon-rules-updater/1.0"},
    )
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.getcode()
                if status != 200:
                    raise UpdateError(f"HTTP {status} while downloading {url}")
                payload = response.read()

            text = payload.decode("utf-8-sig")
            if not text.strip():
                raise UpdateError(f"empty response from {url}")
            return text
        except (OSError, UnicodeError, urllib.error.URLError, UpdateError) as exc:
            last_error = exc
            if attempt < attempts:
                delay = 2 ** (attempt - 1)
                print(
                    f"Download attempt {attempt}/{attempts} failed for {url}: "
                    f"{exc}; retrying in {delay}s",
                    file=sys.stderr,
                )
                time.sleep(delay)

    raise UpdateError(
        f"failed to download {url} after {attempts} attempts: {last_error}"
    ) from last_error


def parse_v2fly(text: str) -> list[str]:
    """Convert supported domain-list-community entries to Loon rules."""
    rules: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Attributes such as @cn, @!cn and @ads follow the value after
        # whitespace. Inline comments, when present, are handled the same way.
        value_token = line.split("#", 1)[0].split()[0]

        if ":" in value_token:
            prefix, value = value_token.split(":", 1)
            loon_type = V2FLY_TYPE_MAP.get(prefix.lower())
            if loon_type is None:
                print(
                    f"Skipping unsupported v2fly entry: {raw_line}",
                    file=sys.stderr,
                )
                continue
        else:
            loon_type = "DOMAIN-SUFFIX"
            value = value_token

        value = value.strip()
        if value:
            rules.append(f"{loon_type},{value}")

    return rules


def parse_blackmatrix(text: str) -> list[str]:
    """Keep all supported Blackmatrix7 Loon rules, including extra fields."""
    rules: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue

        fields = [field.strip() for field in line.split(",")]
        rule_type = fields[0].upper()
        if rule_type not in SUPPORTED_LOON_TYPES:
            continue
        if len(fields) < 2 or not fields[1]:
            print(f"Skipping malformed Blackmatrix7 rule: {raw_line}", file=sys.stderr)
            continue

        # The upstream line is deliberately retained rather than reconstructed,
        # so modifiers such as no-resolve and USER-AGENT patterns stay intact.
        rules.append(line)

    return rules


def rule_key(rule: str) -> tuple[str, ...]:
    """Return a semantic key without conflating different Loon rule types."""
    fields = [field.strip() for field in rule.split(",")]
    fields[0] = fields[0].upper()
    if fields[0] in DOMAIN_TYPES and len(fields) > 1:
        fields[1] = fields[1].lower()
    return tuple(fields)


def deduplicate(rules: Iterable[str]) -> list[str]:
    """Remove exact semantic duplicates while preserving source order."""
    unique: list[str] = []
    seen: set[tuple[str, ...]] = set()

    for rule in rules:
        key = rule_key(rule)
        if key in seen:
            continue
        seen.add(key)
        unique.append(rule)

    return unique


def merge_rules(v2fly_rules: Iterable[str], blackmatrix_rules: Iterable[str]) -> list[str]:
    """Prefer Blackmatrix7 spelling/order, then append unique v2fly rules."""
    return deduplicate([*blackmatrix_rules, *v2fly_rules])


def build_output(rules: list[str], updated_at: datetime | None = None) -> str:
    """Build the complete generated file."""
    timestamp = updated_at or datetime.now(timezone.utc)
    timestamp_text = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = [
        HEADER_TITLE,
        f"{UPDATED_PREFIX}{timestamp_text}",
        f"{TOTAL_PREFIX}{len(rules)}",
        "",
    ]
    return "\n".join([*header, *rules]) + "\n"


def is_current_output(existing: str, rules: list[str]) -> bool:
    """Check material content while retaining the last real update timestamp."""
    lines = existing.splitlines()
    if len(lines) < 4:
        return False
    if lines[0] != HEADER_TITLE or not lines[1].startswith(UPDATED_PREFIX):
        return False
    if lines[2] != f"{TOTAL_PREFIX}{len(rules)}" or lines[3] != "":
        return False
    return lines[4:] == rules


def write_atomic(path: Path, content: str) -> None:
    """Atomically replace the destination after all processing succeeds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def update_rules(
    output_path: Path = OUTPUT_PATH,
    fetcher: Callable[[str], str] = download_text,
    updated_at: datetime | None = None,
) -> bool:
    """Fetch both sources and update output_path. Return whether it changed."""
    # Nothing on disk is touched until both downloads, parsers, and validations
    # have completed successfully.
    v2fly_text = fetcher(V2FLY_URL)
    blackmatrix_text = fetcher(BLACKMATRIX_URL)

    v2fly_rules = parse_v2fly(v2fly_text)
    blackmatrix_rules = parse_blackmatrix(blackmatrix_text)

    if len(v2fly_rules) < MIN_V2FLY_RULES:
        raise UpdateError(
            f"v2fly yielded only {len(v2fly_rules)} rules; "
            f"expected at least {MIN_V2FLY_RULES}"
        )
    if len(blackmatrix_rules) < MIN_BLACKMATRIX_RULES:
        raise UpdateError(
            f"Blackmatrix7 yielded only {len(blackmatrix_rules)} rules; "
            f"expected at least {MIN_BLACKMATRIX_RULES}"
        )

    merged_rules = merge_rules(v2fly_rules, blackmatrix_rules)
    if not merged_rules:
        raise UpdateError("merged rule set is empty")

    if output_path.exists():
        existing = output_path.read_text(encoding="utf-8-sig")
        if is_current_output(existing, merged_rules):
            print(
                f"No rule changes ({len(merged_rules)} rules); keeping existing file"
            )
            return False

    write_atomic(output_path, build_output(merged_rules, updated_at))
    print(
        f"Updated {output_path} with {len(merged_rules)} rules "
        f"({len(blackmatrix_rules)} Blackmatrix7, {len(v2fly_rules)} v2fly before dedup)"
    )
    return True


def main() -> int:
    try:
        update_rules()
    except Exception as exc:
        print(f"YouTube rule update failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

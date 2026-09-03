#!/usr/bin/env python3
"""Update all Loon rule services declared in config/rules.json."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config" / "rules.json"

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
SUPPORTED_FORMATS = {"loon-list", "v2fly-domain-list"}
V2FLY_INCLUDE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._!@+-]*$")

UPDATED_PREFIX = "# 自动更新时间: "
TOTAL_PREFIX = "# 总规则数: "


class UpdateError(RuntimeError):
    """Raised when configuration or upstream data cannot be used safely."""


@dataclass(frozen=True)
class V2flyEntry:
    type_name: str
    value: str
    attributes: tuple[str, ...] = ()
    source_categories: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, tuple[str, ...]]:
        return self.type_name, self.value, self.attributes


@dataclass(frozen=True)
class V2flyInclude:
    name: str
    must_attributes: tuple[str, ...] = ()
    banned_attributes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceConfig:
    name: str
    url: str
    format_name: str
    min_rules: int
    include_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    output: Path
    header: str
    sources: tuple[SourceConfig, ...]
    exclude_includes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedService:
    service: ServiceConfig
    output_path: Path
    rules: tuple[str, ...]
    source_counts: tuple[tuple[str, int], ...]


def download_text(url: str, attempts: int = 3, timeout: int = 30) -> str:
    """Download UTF-8 text with bounded retries."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "loon-rules-updater/2.0"},
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


def parse_v2fly_entry(raw_line: str) -> V2flyEntry | V2flyInclude | None:
    """Parse one v2fly entry while retaining attributes for include filters."""
    line = raw_line.split("#", 1)[0].strip()
    if not line:
        return None

    parts = line.split()
    value_token = parts[0]
    extra_fields = parts[1:]

    if ":" in value_token:
        prefix, value = value_token.split(":", 1)
        prefix = prefix.lower()
        if prefix == "include":
            must_attributes: list[str] = []
            banned_attributes: list[str] = []
            for field in extra_fields:
                if not field.startswith("@") or len(field) == 1:
                    raise UpdateError(f"invalid v2fly include field: {field!r}")
                attribute = field[1:].lower()
                if attribute.startswith("-"):
                    if len(attribute) == 1:
                        raise UpdateError("empty banned v2fly include attribute")
                    banned_attributes.append(attribute[1:])
                else:
                    must_attributes.append(attribute)
            return V2flyInclude(
                name=value.lower(),
                must_attributes=tuple(sorted(must_attributes)),
                banned_attributes=tuple(sorted(banned_attributes)),
            )
        if prefix not in {*V2FLY_TYPE_MAP, "regexp"}:
            print(f"Skipping unsupported v2fly entry: {raw_line}", file=sys.stderr)
            return None
    else:
        prefix = "domain"
        value = value_token

    value = value.strip()
    if not value:
        return None
    if prefix != "regexp":
        value = value.lower()

    attributes: list[str] = []
    for field in extra_fields:
        if field.startswith("@") and len(field) > 1:
            attributes.append(field[1:].lower())
        elif field.startswith("&") and len(field) > 1:
            # Affiliations add this same entry to another list in the upstream
            # build. The entry still belongs to its declaring list.
            continue
        else:
            raise UpdateError(f"invalid v2fly entry field: {field!r}")
    return V2flyEntry(prefix, value, tuple(sorted(attributes)))


def convert_v2fly_entries(entries: Iterable[V2flyEntry]) -> list[str]:
    """Convert supported v2fly entries to Loon after discarding attributes."""
    rules: list[str] = []
    for entry in entries:
        loon_type = V2FLY_TYPE_MAP.get(entry.type_name)
        if loon_type is None:
            print(
                f"Skipping unsupported v2fly entry: {entry.type_name}:{entry.value}",
                file=sys.stderr,
            )
            continue
        rules.append(f"{loon_type},{entry.value}")
    return deduplicate(rules)


def parse_v2fly(text: str) -> list[str]:
    """Convert direct domain-list-community entries, excluding includes."""
    entries: list[V2flyEntry] = []
    for raw_line in text.splitlines():
        entry = parse_v2fly_entry(raw_line)
        if isinstance(entry, V2flyEntry):
            entries.append(entry)
    return convert_v2fly_entries(entries)


def resolve_v2fly_include_url(current_url: str, include_name: str) -> str:
    """Resolve a v2fly include to a sibling file in the same data directory."""
    if not V2FLY_INCLUDE_NAME.fullmatch(include_name) or ".." in include_name:
        raise UpdateError(f"invalid v2fly include name: {include_name!r}")
    encoded_name = urllib.parse.quote(include_name, safe="!@+._-")
    return urllib.parse.urljoin(current_url, encoded_name)


def matches_v2fly_include(entry: V2flyEntry, inclusion: V2flyInclude) -> bool:
    """Apply v2fly's must-have and banned attribute include filters."""
    attributes = set(entry.attributes)
    if not attributes:
        return not inclusion.must_attributes
    if not set(inclusion.must_attributes).issubset(attributes):
        return False
    return not set(inclusion.banned_attributes).intersection(attributes)


def merge_v2fly_entry_sources(
    existing: V2flyEntry | None, entry: V2flyEntry
) -> V2flyEntry:
    """Merge provenance without changing the semantic v2fly entry key."""
    if existing is None:
        return entry
    source_categories = tuple(
        dict.fromkeys((*existing.source_categories, *entry.source_categories))
    )
    return V2flyEntry(
        entry.type_name,
        entry.value,
        entry.attributes,
        source_categories,
    )


def polish_v2fly_entries(entries: Iterable[V2flyEntry]) -> list[V2flyEntry]:
    """Apply v2fly's parent-domain pruning while attributes are still present."""
    rough_entries: dict[tuple[str, str, tuple[str, ...]], V2flyEntry] = {}
    for entry in entries:
        rough_entries[entry.key] = merge_v2fly_entry_sources(
            rough_entries.get(entry.key), entry
        )
    parents: set[str] = set()

    for entry in rough_entries.values():
        if entry.type_name != "domain":
            continue
        parents.add(entry.value)
        if entry.attributes:
            suffix = ":" + ",".join(f"@{attr}" for attr in entry.attributes)
            parents.add(entry.value + suffix)

    polished: list[V2flyEntry] = []
    for entry in rough_entries.values():
        if entry.type_name in {"regexp", "keyword"}:
            polished.append(entry)
            continue
        if entry.type_name not in {"domain", "full"}:
            continue

        if entry.attributes:
            suffix = ":" + ",".join(f"@{attr}" for attr in entry.attributes)
            parent_candidate = entry.value + suffix
        else:
            parent_candidate = entry.value
        if entry.type_name == "full":
            parent_candidate = "." + parent_candidate

        redundant = False
        while "." in parent_candidate:
            parent_candidate = parent_candidate.split(".", 1)[1]
            if parent_candidate in parents:
                redundant = True
                break
        if not redundant:
            polished.append(entry)

    return polished


def expand_v2fly_entries(
    source_url: str,
    fetcher: Callable[[str], str],
    download_cache: dict[str, str] | None = None,
    expansion_cache: dict[
        tuple[str, frozenset[str]], tuple[V2flyEntry, ...]
    ] | None = None,
    exclude_includes: Iterable[str] = (),
) -> list[V2flyEntry]:
    """Recursively resolve allowed includes and return polished v2fly entries."""
    downloads = download_cache if download_cache is not None else {}
    expansions = expansion_cache if expansion_cache is not None else {}
    excluded = frozenset(name.casefold() for name in exclude_includes)

    def visit(url: str, active_urls: tuple[str, ...]) -> tuple[V2flyEntry, ...]:
        if url in active_urls:
            cycle_start = active_urls.index(url)
            cycle = [*active_urls[cycle_start:], url]
            raise UpdateError(f"circular v2fly include: {' -> '.join(cycle)}")
        cache_key = (url, excluded)
        if cache_key in expansions:
            return expansions[cache_key]
        if url not in downloads:
            downloads[url] = fetcher(url)

        entries: dict[tuple[str, str, tuple[str, ...]], V2flyEntry] = {}
        source_category = urllib.parse.unquote(
            urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        ).casefold()
        next_active = (*active_urls, url)
        for raw_line in downloads[url].splitlines():
            entry = parse_v2fly_entry(raw_line)
            if entry is None:
                continue
            if isinstance(entry, V2flyInclude):
                if entry.name.casefold() in excluded:
                    continue
                include_url = resolve_v2fly_include_url(url, entry.name)
                for included_entry in visit(include_url, next_active):
                    if matches_v2fly_include(included_entry, entry):
                        entries[included_entry.key] = merge_v2fly_entry_sources(
                            entries.get(included_entry.key), included_entry
                        )
            else:
                sourced_entry = V2flyEntry(
                    entry.type_name,
                    entry.value,
                    entry.attributes,
                    (source_category,),
                )
                entries[entry.key] = merge_v2fly_entry_sources(
                    entries.get(entry.key), sourced_entry
                )

        expansions[cache_key] = tuple(entries.values())
        return expansions[cache_key]

    return polish_v2fly_entries(visit(source_url, ()))


def expand_v2fly_source(
    source_url: str,
    fetcher: Callable[[str], str],
    download_cache: dict[str, str] | None = None,
    expansion_cache: dict[
        tuple[str, frozenset[str]], tuple[V2flyEntry, ...]
    ] | None = None,
    exclude_includes: Iterable[str] = (),
) -> list[str]:
    """Expand allowed includes, polish, convert, strip attributes, and deduplicate."""
    entries = expand_v2fly_entries(
        source_url,
        fetcher,
        download_cache,
        expansion_cache,
        exclude_includes,
    )
    return convert_v2fly_entries(entries)


def parse_loon(text: str, include_types: Iterable[str]) -> list[str]:
    """Keep configured Loon rule types, including all extra fields."""
    allowed_types = {rule_type.upper() for rule_type in include_types}
    rules: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue

        fields = [field.strip() for field in line.split(",")]
        rule_type = fields[0].upper()
        if rule_type not in allowed_types:
            continue
        if len(fields) < 2 or not fields[1]:
            print(f"Skipping malformed Loon rule: {raw_line}", file=sys.stderr)
            continue

        # Retain the upstream line so modifiers such as no-resolve and
        # USER-AGENT patterns stay intact.
        rules.append(line)

    return rules


def parse_source(source: SourceConfig, text: str) -> list[str]:
    if source.format_name == "v2fly-domain-list":
        return parse_v2fly(text)
    if source.format_name == "loon-list":
        return parse_loon(text, source.include_types)
    raise UpdateError(f"unsupported source format: {source.format_name}")


def rule_key(rule: str) -> tuple[str, ...]:
    """Return a semantic key without conflating different Loon rule types."""
    fields = [field.strip() for field in rule.split(",")]
    fields[0] = fields[0].upper()
    if fields[0] in DOMAIN_TYPES and len(fields) > 1:
        fields[1] = fields[1].lower()
    return tuple(fields)


def deduplicate(rules: Iterable[str]) -> list[str]:
    """Remove semantic duplicates while preserving source order."""
    unique: list[str] = []
    seen: set[tuple[str, ...]] = set()

    for rule in rules:
        key = rule_key(rule)
        if key in seen:
            continue
        seen.add(key)
        unique.append(rule)

    return unique


def _required_string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise UpdateError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _parse_source(data: Any, context: str) -> SourceConfig:
    if not isinstance(data, dict):
        raise UpdateError(f"{context} must be an object")

    name = _required_string(data, "name", context)
    url = _required_string(data, "url", context)
    format_name = _required_string(data, "format", context)
    min_rules = data.get("min_rules")

    if not url.startswith("https://"):
        raise UpdateError(f"{context}.url must use HTTPS")
    if format_name not in SUPPORTED_FORMATS:
        raise UpdateError(
            f"{context}.format must be one of {sorted(SUPPORTED_FORMATS)}"
        )
    if isinstance(min_rules, bool) or not isinstance(min_rules, int) or min_rules < 1:
        raise UpdateError(f"{context}.min_rules must be a positive integer")

    raw_types = data.get("include_types", sorted(SUPPORTED_LOON_TYPES))
    if not isinstance(raw_types, list) or not raw_types:
        raise UpdateError(f"{context}.include_types must be a non-empty list")
    if not all(isinstance(item, str) and item.strip() for item in raw_types):
        raise UpdateError(f"{context}.include_types contains an invalid value")
    include_types = tuple(item.strip().upper() for item in raw_types)
    unsupported_types = set(include_types) - SUPPORTED_LOON_TYPES
    if unsupported_types:
        raise UpdateError(
            f"{context}.include_types contains unsupported types: "
            f"{sorted(unsupported_types)}"
        )

    return SourceConfig(name, url, format_name, min_rules, include_types)


def load_config(path: Path) -> tuple[ServiceConfig, ...]:
    """Load and validate the multi-service JSON configuration."""
    try:
        raw_config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"failed to load configuration {path}: {exc}") from exc

    if not isinstance(raw_config, dict):
        raise UpdateError("configuration root must be an object")
    raw_services = raw_config.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise UpdateError("configuration must contain a non-empty services list")

    services: list[ServiceConfig] = []
    seen_names: set[str] = set()
    seen_outputs: set[str] = set()

    for service_index, raw_service in enumerate(raw_services):
        context = f"services[{service_index}]"
        if not isinstance(raw_service, dict):
            raise UpdateError(f"{context} must be an object")

        name = _required_string(raw_service, "name", context)
        output_text = _required_string(raw_service, "output", context)
        header = _required_string(raw_service, "header", context)
        output = Path(output_text)

        if output.is_absolute() or ".." in output.parts:
            raise UpdateError(f"{context}.output must be a safe relative path")
        if not output.parts or output.parts[0] != "rule" or output.suffix != ".list":
            raise UpdateError(f"{context}.output must be a rule/*.list path")

        name_key = name.casefold()
        output_key = output.as_posix().casefold()
        if name_key in seen_names:
            raise UpdateError(f"duplicate service name: {name}")
        if output_key in seen_outputs:
            raise UpdateError(f"duplicate service output: {output.as_posix()}")
        seen_names.add(name_key)
        seen_outputs.add(output_key)

        raw_sources = raw_service.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise UpdateError(f"{context}.sources must be a non-empty list")
        sources = tuple(
            _parse_source(source, f"{context}.sources[{source_index}]")
            for source_index, source in enumerate(raw_sources)
        )

        raw_excludes = raw_service.get("exclude_includes", [])
        if not isinstance(raw_excludes, list) or not all(
            isinstance(item, str) and item.strip() for item in raw_excludes
        ):
            raise UpdateError(
                f"{context}.exclude_includes must be a list of non-empty strings"
            )
        exclude_includes: list[str] = []
        seen_excludes: set[str] = set()
        for item in raw_excludes:
            include_name = item.strip().casefold()
            if (
                not V2FLY_INCLUDE_NAME.fullmatch(include_name)
                or ".." in include_name
            ):
                raise UpdateError(
                    f"{context}.exclude_includes contains an invalid include name: "
                    f"{item!r}"
                )
            if include_name not in seen_excludes:
                seen_excludes.add(include_name)
                exclude_includes.append(include_name)

        services.append(
            ServiceConfig(name, output, header, sources, tuple(exclude_includes))
        )

    return tuple(services)


def resolve_output(repository_root: Path, relative_path: Path) -> Path:
    root = repository_root.resolve()
    output_path = (root / relative_path).resolve()
    try:
        output_path.relative_to(root)
    except ValueError as exc:
        raise UpdateError(f"output escapes repository root: {relative_path}") from exc
    return output_path


def prepare_services(
    services: Iterable[ServiceConfig],
    repository_root: Path,
    fetcher: Callable[[str], str],
) -> tuple[PreparedService, ...]:
    """Download and validate every service before any file is written."""
    prepared: list[PreparedService] = []
    download_cache: dict[str, str] = {}
    expansion_cache: dict[
        tuple[str, frozenset[str]], tuple[V2flyEntry, ...]
    ] = {}

    for service in services:
        merged: list[str] = []
        source_counts: list[tuple[str, int]] = []

        for source in service.sources:
            if source.format_name == "v2fly-domain-list":
                rules = expand_v2fly_source(
                    source.url,
                    fetcher,
                    download_cache,
                    expansion_cache,
                    service.exclude_includes,
                )
            else:
                if source.url not in download_cache:
                    download_cache[source.url] = fetcher(source.url)
                rules = parse_source(source, download_cache[source.url])
            if len(rules) < source.min_rules:
                raise UpdateError(
                    f"{service.name}/{source.name} yielded only {len(rules)} rules; "
                    f"expected at least {source.min_rules}"
                )
            merged.extend(rules)
            source_counts.append((source.name, len(rules)))

        unique_rules = deduplicate(merged)
        if not unique_rules:
            raise UpdateError(f"{service.name} merged rule set is empty")
        prepared.append(
            PreparedService(
                service=service,
                output_path=resolve_output(repository_root, service.output),
                rules=tuple(unique_rules),
                source_counts=tuple(source_counts),
            )
        )

    return tuple(prepared)


def build_output(
    service: ServiceConfig,
    rules: Iterable[str],
    updated_at: datetime | None = None,
) -> str:
    """Build one complete generated rule file."""
    rule_list = list(rules)
    timestamp = updated_at or datetime.now(timezone.utc)
    timestamp_text = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = [
        f"# {service.header}",
        f"{UPDATED_PREFIX}{timestamp_text}",
        f"{TOTAL_PREFIX}{len(rule_list)}",
        "",
    ]
    return "\n".join([*header, *rule_list]) + "\n"


def is_current_output(
    existing: str,
    service: ServiceConfig,
    rules: Iterable[str],
) -> bool:
    """Compare material content while retaining the last update timestamp."""
    rule_list = list(rules)
    lines = existing.splitlines()
    if len(lines) < 4:
        return False
    if lines[0] != f"# {service.header}" or not lines[1].startswith(UPDATED_PREFIX):
        return False
    if lines[2] != f"{TOTAL_PREFIX}{len(rule_list)}" or lines[3] != "":
        return False
    return lines[4:] == rule_list


def write_atomic(path: Path, content: str) -> None:
    """Atomically replace one destination after preparation succeeds."""
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


def update_all(
    config_path: Path = DEFAULT_CONFIG_PATH,
    repository_root: Path = REPOSITORY_ROOT,
    fetcher: Callable[[str], str] = download_text,
    updated_at: datetime | None = None,
) -> tuple[Path, ...]:
    """Update every configured service and return the changed output paths."""
    services = load_config(config_path)
    prepared_services = prepare_services(services, repository_root, fetcher)
    changed: list[Path] = []

    for prepared in prepared_services:
        path = prepared.output_path
        if path.exists():
            existing = path.read_text(encoding="utf-8-sig")
            if is_current_output(existing, prepared.service, prepared.rules):
                print(
                    f"{prepared.service.name}: no rule changes "
                    f"({len(prepared.rules)} rules)"
                )
                continue

        write_atomic(
            path,
            build_output(prepared.service, prepared.rules, updated_at),
        )
        counts = ", ".join(
            f"{name}={count}" for name, count in prepared.source_counts
        )
        print(
            f"{prepared.service.name}: updated {path} with "
            f"{len(prepared.rules)} rules ({counts}, before dedup)"
        )
        changed.append(path)

    if not changed:
        print("All configured rule files are already up to date")
    return tuple(changed)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"service configuration (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        update_all(config_path=args.config)
    except Exception as exc:
        print(f"Rule update failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

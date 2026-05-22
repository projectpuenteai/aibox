"""
Scan manifests/release-config.yaml for Kiwix items and update them to
the latest available dump dates from the download.kiwix.org directory listing.

Uses a directory scrape of https://download.kiwix.org/zim/wikipedia/ —
the OPDS name filter is unreliable and not used here.

For each Kiwix item the script:
  1. Reads the catalog_query.name (e.g. "wikipedia_en_all_mini").
  2. Fetches https://download.kiwix.org/zim/wikipedia/ and finds the
     latest  <name>_YYYY-MM.zim file by parsing the directory listing.
  3. Updates fallback_url, sha256_url, and target in release-config.yaml,
     preserving all comments and ordering (via ruamel.yaml, or a simple
     regex fallback if ruamel is not installed).

Usage
-----
    python build/rotate_kiwix_dates.py           # apply updates in-place
    python build/rotate_kiwix_dates.py --dry-run # print changes, don't write

Requirements
------------
    pip install requests ruamel.yaml   (ruamel.yaml is optional but recommended)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package not installed.  Run: pip install requests", file=sys.stderr)
    sys.exit(1)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "manifests" / "release-config.yaml"
_KIWIX_BASE = "https://download.kiwix.org/zim/wikipedia/"


def fetch_directory_listing(url: str) -> str:
    """Return the raw HTML body of a directory listing page."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_latest_date(html: str, name: str) -> Optional[str]:
    """
    Given the HTML of the Kiwix wikipedia/ directory listing, find the latest
    YYYY-MM date string for files matching  <name>_YYYY-MM.zim.

    Returns the YYYY-MM string (e.g. "2026-03") or None if no match found.
    """
    pattern = re.compile(
        r'href="' + re.escape(name) + r'_(\d{4}-\d{2})\.zim"',
        re.IGNORECASE,
    )
    dates = pattern.findall(html)
    if not dates:
        return None
    return sorted(dates)[-1]


def update_kiwix_item_regex(text: str, name: str, old_date: str, new_date: str) -> str:
    """
    Replace all occurrences of <name>_<old_date> with <name>_<new_date>
    in the YAML text.  This is the fallback path when ruamel.yaml is absent.
    """
    old_fragment = f"{name}_{old_date}"
    new_fragment = f"{name}_{new_date}"
    return text.replace(old_fragment, new_fragment)


def load_yaml_ruamel(path: Path):
    from ruamel.yaml import YAML  # type: ignore
    yaml = YAML()
    yaml.preserve_quotes = True
    with path.open("r", encoding="utf-8") as fh:
        return yaml, yaml.load(fh)


def save_yaml_ruamel(yaml, data, path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print proposed changes without modifying release-config.yaml.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_CONFIG_PATH,
        help=f"Path to release-config.yaml (default: {_CONFIG_PATH})",
    )
    args = parser.parse_args()

    config_path: Path = args.config
    if not config_path.exists():
        print(f"ERROR: Config not found: {config_path}", file=sys.stderr)
        return 1

    # Determine whether ruamel.yaml is available.
    try:
        from ruamel.yaml import YAML as _YAML  # noqa: F401
        have_ruamel = True
    except ImportError:
        have_ruamel = False
        print(
            "WARNING: ruamel.yaml not installed — falling back to regex replacement.\n"
            "         Install ruamel.yaml for comment-preserving updates:\n"
            "         pip install ruamel.yaml",
            file=sys.stderr,
        )

    # Parse config to find Kiwix items.
    if have_ruamel:
        yaml_obj, data = load_yaml_ruamel(config_path)
        items = data.get("items", [])
    else:
        yaml_obj = None
        data = None
        raw_text = config_path.read_text(encoding="utf-8")
        items = _parse_kiwix_items_regex(raw_text)

    # Fetch the directory listing once.
    print(f"Fetching {_KIWIX_BASE} ...")
    try:
        html = fetch_directory_listing(_KIWIX_BASE)
    except Exception as exc:
        print(f"ERROR: Could not fetch Kiwix directory: {exc}", file=sys.stderr)
        return 1

    any_change = False

    if have_ruamel:
        for item in items:
            if item.get("source") != "kiwix":
                continue
            cq = item.get("catalog_query", {})
            name = cq.get("name")
            current_date = str(cq.get("date", ""))
            if not name:
                continue

            latest = find_latest_date(html, name)
            if not latest:
                print(f"  [{item.get('id', name)}] No matching file found on server — skipping.")
                continue

            if latest == current_date:
                print(f"  [{item.get('id', name)}] Already at latest: {current_date}")
                continue

            print(f"  [{item.get('id', name)}] {current_date} -> {latest}")
            any_change = True

            if not args.dry_run:
                cq["date"] = latest
                for field in ("fallback_url", "sha256_url", "target"):
                    val = item.get(field)
                    if val and current_date in str(val):
                        item[field] = str(val).replace(
                            f"{name}_{current_date}", f"{name}_{latest}"
                        )

        if any_change and not args.dry_run:
            save_yaml_ruamel(yaml_obj, data, config_path)
            print(f"\nUpdated: {config_path}")
        elif args.dry_run and any_change:
            print("\nDry-run mode — no files written.")
        elif not any_change:
            print("\nAll Kiwix items are already at the latest available dates.")

    else:
        # Regex fallback: parse item names and dates from raw text.
        raw_text = config_path.read_text(encoding="utf-8")
        updated_text = raw_text

        for name, current_date in items:
            latest = find_latest_date(html, name)
            if not latest:
                print(f"  [{name}] No matching file found on server — skipping.")
                continue
            if latest == current_date:
                print(f"  [{name}] Already at latest: {current_date}")
                continue

            print(f"  [{name}] {current_date} -> {latest}")
            any_change = True
            if not args.dry_run:
                updated_text = update_kiwix_item_regex(updated_text, name, current_date, latest)

        if any_change and not args.dry_run:
            config_path.write_text(updated_text, encoding="utf-8")
            print(f"\nUpdated: {config_path}")
        elif args.dry_run and any_change:
            print("\nDry-run mode — no files written.")
        elif not any_change:
            print("\nAll Kiwix items are already at the latest available dates.")

    return 0


def _parse_kiwix_items_regex(text: str) -> list[tuple[str, str]]:
    """
    Extract (catalog_query.name, date) pairs from raw YAML text.
    Used only when ruamel.yaml is unavailable.
    """
    results: list[tuple[str, str]] = []
    # Match adjacent name/date lines under catalog_query blocks.
    name_pat = re.compile(r"name:\s*(\S+)")
    date_pat = re.compile(r"date:\s*(\d{4}-\d{2})")

    names_found: list[str] = name_pat.findall(text)
    dates_found: list[str] = date_pat.findall(text)

    # Pair them by order of appearance (relies on yaml structure being consistent).
    for name, date in zip(names_found, dates_found):
        results.append((name, date))
    return results


if __name__ == "__main__":
    sys.exit(main())

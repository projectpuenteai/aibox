#!/usr/bin/env python3
"""
Download the Kolibri documentation screenshots used by the teacher guide.

Run this from the images/ directory:

    python download.py

It uses only the Python standard library (urllib) and saves the images
next to itself. Existing files are not overwritten unless --force is passed.

Screenshots come from the Kolibri User Guide, published by Learning Equality
under CC BY-SA 4.0. See README.md in this folder for attribution.
"""

import argparse
import os
import sys
import urllib.request
from urllib.error import URLError, HTTPError

# Mapping of local filename -> source URL.
# Keep this list in sync with images/README.md and the <img> tags in the HTML guides.
IMAGES = {
    "create-account.png":   "https://kolibri.readthedocs.io/en/latest/_images/create-account.png",
    "manage-users.png":     "https://kolibri.readthedocs.io/en/latest/_images/manage-users.png",
    "coach-type.png":       "https://kolibri.readthedocs.io/en/latest/_images/coach-type.png",
    "groups-home.png":      "https://kolibri.readthedocs.io/en/latest/_images/groups-home.png",
    "learner-groups.png":   "https://kolibri.readthedocs.io/en/latest/_images/learner-groups.png",
    "lessons-home.png":     "https://kolibri.readthedocs.io/en/latest/_images/lessons-home.png",
    "lesson-visible.png":   "https://kolibri.readthedocs.io/en/latest/_images/lesson-visible.png",
    "quizzes-home.png":     "https://kolibri.readthedocs.io/en/latest/_images/quizzes-home.png",
    "coach-home.png":       "https://kolibri.readthedocs.io/en/latest/_images/coach-home.png",
    "learners-home.png":    "https://kolibri.readthedocs.io/en/latest/_images/learners-home.png",
}


def download(url: str, target: str) -> None:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Kolibri-Teacher-Guide-Image-Downloader/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as resp, open(target, "wb") as out:
        out.write(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="Re-download images even if the local file already exists.")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    failures = 0
    for name, url in IMAGES.items():
        target = os.path.join(here, name)
        if os.path.exists(target) and not args.force:
            print(f"  ok    {name} (already present, skipping)")
            continue
        print(f"  fetch {name}", flush=True)
        try:
            download(url, target)
        except (URLError, HTTPError) as exc:
            print(f"  FAIL  {name}: {exc}", file=sys.stderr)
            failures += 1
        except OSError as exc:
            print(f"  FAIL  {name}: {exc}", file=sys.stderr)
            failures += 1

    if failures:
        print(f"\n{failures} image(s) failed to download.", file=sys.stderr)
        return 1
    print("\nAll done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

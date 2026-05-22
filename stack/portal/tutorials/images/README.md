# Kolibri Teacher Guide — Image Assets

This folder holds the screenshots referenced by `kolibri_teacher_guide_es.html` and `kolibri_teacher_guide_en.html`.

## Sources and licensing

All images come from the official Kolibri User Guide, hosted on Read the Docs:

- https://kolibri.readthedocs.io/en/latest/  (English)
- https://kolibri.readthedocs.io/es/latest/  (Spanish)

The Kolibri documentation is published by Learning Equality under the
[Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)](https://creativecommons.org/licenses/by-sa/4.0/) license.
You may copy, redistribute, and adapt the screenshots, provided that you:

1. Keep the attribution to Learning Equality / the Kolibri project.
2. Distribute any adapted material under the same CC BY-SA 4.0 license.

The HTML guide includes the attribution in its footer; please keep it.

## How to populate this folder

If the images are missing, the HTML guide will still render — the `<img>` tags fall
back to a captioned placeholder showing the expected filename. To get the real
screenshots, run one of the scripts below from this `images/` directory.

### Option 1 — Python (cross-platform)

```bash
python download.py
```

Requires Python 3.6+ (no external libraries).

### Option 2 — Bash / curl (Linux, macOS, Git Bash)

```bash
bash download.sh
```

### Option 3 — PowerShell (Windows)

```powershell
./download.ps1
```

After the script finishes, refresh the HTML guide in your browser; the placeholders
will be replaced with the actual screenshots.

## File manifest

Filename and the source URL that the scripts download from. Captions describe what each screenshot shows.

| Filename | Source URL | Caption |
|---|---|---|
| `create-account.png`   | https://kolibri.readthedocs.io/en/latest/_images/create-account.png   | Sign-in / account-creation screen |
| `manage-users.png`     | https://kolibri.readthedocs.io/en/latest/_images/manage-users.png     | Users tab inside Facility |
| `coach-type.png`       | https://kolibri.readthedocs.io/en/latest/_images/coach-type.png       | Class-coach vs Facility-coach selector |
| `groups-home.png`      | https://kolibri.readthedocs.io/en/latest/_images/groups-home.png      | Groups tab in coach dashboard |
| `learner-groups.png`   | https://kolibri.readthedocs.io/en/latest/_images/learner-groups.png   | Enrolling students into a group |
| `lessons-home.png`     | https://kolibri.readthedocs.io/en/latest/_images/lessons-home.png     | Lessons tab in coach dashboard |
| `lesson-visible.png`   | https://kolibri.readthedocs.io/en/latest/_images/lesson-visible.png   | Lesson detail with the visibility switch |
| `quizzes-home.png`     | https://kolibri.readthedocs.io/en/latest/_images/quizzes-home.png     | Quizzes tab in coach dashboard |
| `coach-home.png`       | https://kolibri.readthedocs.io/en/latest/_images/coach-home.png       | Coach dashboard with the class list |
| `learners-home.png`    | https://kolibri.readthedocs.io/en/latest/_images/learners-home.png    | Learners tab — per-student progress |

If Kolibri's documentation is updated and image URLs change, you can update the manifest
here and the scripts will pick up the new URLs on the next run.

## License notice for redistribution

If you redistribute these screenshots, include in your distribution:

> Screenshots from the Kolibri User Guide by Learning Equality,
> https://kolibri.readthedocs.io/, used under CC BY-SA 4.0.

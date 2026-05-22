# Kolibri Teacher Guide

Bilingual, offline-ready HTML guide for teachers using Kolibri in classroom
deployments. Designed for Spanish-speaking teachers in Guatemala (primary
audience) plus an English version for internal/partner review.

## What's in this folder

```
.
├── kolibri_teacher_guide_es.html   ← Spanish version (primary)
├── kolibri_teacher_guide_en.html   ← English version (for the team)
├── README.md                       ← this file
└── images/
    ├── README.md                   ← attribution + image list
    ├── download.py                 ← Python downloader (recommended)
    ├── download.sh                 ← Bash/curl downloader
    ├── download.ps1                ← PowerShell downloader (Windows)
    └── *.png                       ← screenshots (after running a downloader)
```

Each HTML file is **fully self-contained** — CSS, JavaScript, and content live
inside the same file. The only external assets are the screenshots in `images/`,
and even those degrade gracefully to captioned placeholders if missing.

## Deployment

1. Copy the entire folder (`kolibri_teacher_guide_es.html`,
   `kolibri_teacher_guide_en.html`, and `images/`) to each device or to a
   shared location accessible from the school's network.
2. From inside the `images/` directory, run one of the downloader scripts
   **once** on a machine with internet access to populate the PNG screenshots:

   ```bash
   cd images && python download.py
   ```

   After this you can copy the populated folder offline to all target devices.
3. Teachers open the HTML file in any browser (Chrome, Firefox, Edge). No
   server required.

The pages are designed to work on low-power laptops with small screens
and to print cleanly as a paper handout.

## Features

- Top navigation bar with sticky search input
- Left sidebar with section index (collapses to a button on mobile)
- "Most common tasks" cards near the top
- Step-by-step instructions with numbered bullets
- Callouts for *Notes*, *Tips*, *Warnings*
- Screenshots from the official Kolibri docs, with captions and alt text
- Collapsible troubleshooting items (`<details>`)
- First-day checklist and quick-reference cheat sheet
- Glossary
- Print stylesheet: nav and search hidden, sections on their own pages,
  links spelled out in parentheses
- Accessibility:
  - Semantic landmarks (`header`, `nav`, `main`, `aside`, `footer`)
  - Skip-to-content link
  - `alt` text on every image, captioned figures
  - `aria-label` / `aria-expanded` on the menu toggle
  - Visible focus rings, high color contrast
  - Keyboard navigable; the search input supports `Esc` to clear

## Content style

- **Spanish version** — neutral Latin American Spanish, *usted*, practical
  classroom examples. Avoids Spain-specific phrasing.
- **English version** — natural translation, same structure, same screenshots.

Topics covered in both:

1. What Kolibri is and what it's used for
2. Signing in and signing out
3. Account roles (Learner, Coach, Facility Admin, Super Admin) with a clear
   note that **teachers should not be Super Admins** in this deployment
4. Creating student accounts
5. Creating teacher / coach accounts (with Class coach vs Facility coach)
6. Creating a class
7. Enrolling students in a class and assigning a coach
8. Creating groups inside a class
9. Assigning lessons (creation, adding resources, visibility, copy)
10. Creating quizzes (creation, scoring options, starting/ending)
11. Checking student progress (Class Home, Learners tab, Difficult questions,
    print/export)
12. Helping students find content in the Library
13. First-day checklist
14. Troubleshooting (login, missing students, content, page won't load,
    role confusion, sync delays)
15. Quick reference cheat sheet
16. Short glossary

## Documentation pages referenced

The content is grounded in the official Kolibri User Guide. The specific
pages used as sources:

- https://kolibri.readthedocs.io/en/latest/manage/user_roles.html  — roles
- https://kolibri.readthedocs.io/en/latest/manage/users.html  — user CRUD
- https://kolibri.readthedocs.io/en/latest/manage/classes.html  — classes
- https://kolibri.readthedocs.io/en/latest/coach/index.html  — coach dashboard
- https://kolibri.readthedocs.io/en/latest/coach/lessons.html  — lessons
- https://kolibri.readthedocs.io/en/latest/coach/quizzes.html  — quizzes
- https://kolibri.readthedocs.io/en/latest/coach/learners.html  — learners
- https://kolibri.readthedocs.io/en/latest/coach/groups.html  — groups
- https://kolibri.readthedocs.io/en/latest/learn.html  — student-side flows
- Spanish mirror: https://kolibri.readthedocs.io/es/latest/

Where the Spanish docs use a localized term (e.g. *Tutor* for *Coach*,
*Centro* for *Facility*), the guide uses the same term and explains it once.

## Assumptions about this deployment

- Kolibri is **already installed, configured, and running** on the school's
  computers / server. The guide does **not** cover install, server setup,
  channel import, or admin device settings.
- Teachers are **not Super Admins**. If a screen asks them to change device
  settings, import channels, or edit permissions, the guide tells them to
  stop and call the project's technical team.
- Most teachers will be **Class coaches**. A school may have one or two
  **Facility admins** (often a director or coordinator) who create users
  and classes. Where instructions require admin-level access, that's stated
  explicitly so coaches know who to ask.
- Some content (e.g. quiz reporting visibility) was sourced from the
  English docs because the Spanish docs page returned an empty body during
  research; the workflows are identical across languages.

## License notes

- The guide text in `kolibri_teacher_guide_*.html` is yours to use and
  modify freely within your deployment.
- Screenshots referenced in `images/` come from the official Kolibri User
  Guide and are licensed CC BY-SA 4.0. Keep the attribution that's already
  in the HTML footer and in `images/README.md` if you redistribute.
- *Kolibri* and its logo are trademarks of Learning Equality. This guide is
  unaffiliated support material.

## Updating

Two parts to keep in sync if Kolibri's UI changes:

1. **Wording inside the HTML** — search for the button name that changed
   (e.g. `NEW USER`) and update it in both `.html` files.
2. **Screenshots** — re-run `python images/download.py --force` to refresh
   the PNGs from upstream. If a screenshot is added or removed, also update
   the `IMAGES` map in `download.py`, the `IMAGES` array in the bash and
   PowerShell scripts, and the manifest table in `images/README.md`.

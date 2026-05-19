# Legacy Root Stack Archive

`C:\AIBox\not-needed\legacy-root-stack` is not part of the active AIBox runtime.
The active stack lives under `C:\AIBox\aibox`.

## Reference Check

Active repository search found no code or config dependency on
`legacy-root-stack`. References that remain are workspace documentation and the
priority 5 tracking item.

## Why It Remains Outside Git

The directory contains archived legacy binaries, old models, old service files,
prototype UI code, and extracted content. It is large and noisy for searches, but
moving or deleting it is an operator storage decision because it may be the only
local copy of historical artifacts.

## Recommended Archive Procedure

1. Stop the active stack.
2. Copy `C:\AIBox\not-needed\legacy-root-stack` to external archive storage.
3. Verify the archive copy size and file count.
4. Remove the local copy only after archive verification.
5. Keep this document updated with the archive location and date.

Do not place the archive under `C:\AIBox\aibox`; it should remain outside the
active repository path.

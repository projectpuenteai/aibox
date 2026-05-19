# AIBox Maintenance Workflow

AIBox devices are expected to run offline for long stretches. Perform dependency
and vulnerability reviews on an online development machine, then ship only tested
and reversible updates to field devices.

## Monthly Dependency Review

1. Create a branch and snapshot the current working stack.
2. Review Python dependencies:
   ```powershell
   py -3 -m pip install --upgrade pip-audit
   py -3 -m pip_audit -r .\aibox\tools\ai-control\requirements.txt
   py -3 -m pip_audit -r .\aibox\requirements.txt
   ```
3. Review frontend/test dependencies:
   ```powershell
   Push-Location .\aibox\tools\tests
   npm audit
   Pop-Location
   ```
4. Review container images using an image scanner available on the online build
   machine, such as Docker Scout, Trivy, or Grype. Record image digest changes in
   `docs/image-update-policy.md`.
5. Review vendored browser assets under `stack/portal/assets/vendor/` against
   their upstream releases.
6. Apply one dependency family at a time. Avoid mixing Python, image, and
   frontend updates in one field release unless a security fix requires it.

## Required Validation

Run these checks before exporting images or copying updates to an offline device:

```powershell
py -3 -m pip install -r .\aibox\tools\tests\requirements-test.txt
powershell -ExecutionPolicy Bypass -File .\aibox\tools\tests\validate_python.ps1
py -3 -m pytest .\aibox\tools\tests\test_storage_migrations.py
py -3 -m pytest .\aibox\tools\tests\test_security_controls.py
py -3 -m pytest .\aibox\tools\tests\test_rag_durability.py
powershell -ExecutionPolicy Bypass -File .\aibox\tools\tests\compose_config_redacted.ps1
docker compose -f .\aibox\stack\docker-compose.yaml up -d
docker compose -f .\aibox\stack\docker-compose.yaml ps
```

Do not paste raw `docker compose config` output into issues or chat. It expands
real values from `stack/.env`, including deployment secrets.

Then manually verify login, admin status, chat, saved chats, docs, English and
Spanish RAG, Kiwix, Kolibri, and hotspot client access.

## Rollback Notes

- Keep the previous compose file, `.env`, image digests, and `backend-data`
  snapshot until the replacement has passed a storage disaster drill.
- Do not upgrade the live field database without a tested backup and restore
  path.
- If an update fails after deployment, restore the previous code/image bundle and
  rerun the startup preflight before opening the device to students.

## Encoding Hygiene

- Keep hand-written docs and scripts UTF-8 encoded.
- Prefer ASCII punctuation in operational docs and comments unless names or UI
  copy require Unicode.
- When editing PowerShell, write JSON with `Set-Content -Encoding UTF8` or an
  equivalent explicit encoding.
- Do not edit vendored minified assets only to normalize formatting or encoding.

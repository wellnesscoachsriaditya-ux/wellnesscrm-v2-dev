# Test Credentials — WellnessCRM Preview

## Practitioner (Coach) — seeded via real API (`/app/scripts/dev_seed.py`)
- Email: `asha@sunrisenutrition.in`
- Password: `sunrise-wellness-2025`
- Practice: Sunrise Nutrition Studio (owner). 12 real clients + 1 draft nutrition plan.

## Notes for testing/fork agents
- Auth uses the real `/api/v1/public/auth/login`. Credentials live in the backend's
  in-memory local store, so after a **backend restart** they are cleared. To restore:
  1. `sudo supervisorctl restart backend`
  2. `/root/.venv/bin/python /app/scripts/dev_seed.py`  (idempotent: resets tenant data, re-registers, re-seeds)
- Full DB rebuild (only if the database is wiped): `bash /app/scripts/provision_local_db.sh`
  then restart backend and run the seed.
- Postgres (local): db `wellnesscrm`, roles `app_user` / `app_migrator`, password `localdev`.

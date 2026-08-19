"""Development preview seed — REAL data through the real API.

This does NOT fabricate metrics. It drives the actual FastAPI endpoints
(register → activate → login → create clients → create a plan) exactly as a
practitioner would, so every value on screen is server-authoritative and passes
through the real authorization + RLS path.

Idempotent: it truncates the tenant graph first (as the DB superuser, the only
role permitted to), then recreates everything. Because the local credential
store lives in the running server's memory, registration must go through HTTP —
so this must be run against the *running* backend, after any restart.

Usage:  /root/.venv/bin/python /app/scripts/dev_seed.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:8001"
OWNER_EMAIL = "asha@sunrisenutrition.in"
OWNER_PASSWORD = "sunrise-wellness-2025"
TERMS = "2025-05-01"


def sql_superuser(statement: str) -> None:
    subprocess.run(
        ["su", "-", "postgres", "-c", f'psql -d wellnesscrm -v ON_ERROR_STOP=1 -c "{statement}"'],
        check=True,
        capture_output=True,
    )


def sql_file_superuser(path: str) -> None:
    subprocess.run(
        ["su", "-", "postgres", "-c", f"psql -d wellnesscrm -v ON_ERROR_STOP=1 -f {path}"],
        check=True,
        capture_output=True,
    )


def call(method: str, path: str, *, token: str | None = None, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise SystemExit(f"{method} {path} -> {exc.code}\n{detail}") from exc


CLIENTS = [
    # name, mobile, email, stage, sex, city, dietary_class, source
    ("Ananya Sharma", "+919820011234", "ananya.sharma@example.in", "consultation_scheduled", "female", "Mumbai", "vegetarian", "instagram"),
    ("Rohan Mehta", "+919810022345", "rohan.mehta@example.in", "contacted", "male", "Pune", "non_vegetarian", "referral"),
    ("Priya Nair", "+919833033456", "priya.nair@example.in", "consultation_scheduled", "female", "Bengaluru", "eggetarian", "website"),
    ("Vikram Iyer", "+919845044567", "vikram.iyer@example.in", "lead", "male", "Chennai", "vegetarian", "instagram"),
    ("Sneha Kulkarni", "+919867055678", "sneha.k@example.in", "contacted", "female", "Nagpur", "jain", "referral"),
    ("Arjun Reddy", "+919876066789", "arjun.reddy@example.in", "lead", "male", "Hyderabad", "non_vegetarian", "website"),
    ("Meera Joshi", "+919812077890", "meera.joshi@example.in", "consultation_scheduled", "female", "Nashik", "vegan", "instagram"),
    ("Karan Malhotra", "+919823088901", "karan.m@example.in", "contacted", "male", "Delhi", "non_vegetarian", "referral"),
    ("Divya Menon", "+919834099012", "divya.menon@example.in", "lead", "female", "Kochi", "eggetarian", "website"),
    ("Sameer Deshpande", "+919845100123", "sameer.d@example.in", "paused", "male", "Pune", "vegetarian", "referral"),
    ("Fatima Sheikh", "+919856111234", "fatima.s@example.in", "consultation_scheduled", "female", "Mumbai", "non_vegetarian", "instagram"),
    ("Nikhil Verma", "+919867122345", "nikhil.verma@example.in", "lead", "male", "Jaipur", "vegetarian", "website"),
]


def main() -> None:
    print("Resetting tenant graph…", flush=True)
    sql_superuser("TRUNCATE tenants CASCADE")

    # 🔒 TRUNCATE ... CASCADE also empties the curated catalogue tables (they FK
    # to tenants), so restore it here — every seed leaves a usable food catalogue.
    print("Restoring curated food catalogue…", flush=True)
    sql_file_superuser("/app/ops/db/seed_food_catalogue.sql")

    print("Registering practice…", flush=True)
    call(
        "POST",
        "/api/v1/public/auth/register",
        body={
            "email": OWNER_EMAIL,
            "password": OWNER_PASSWORD,
            "full_name": "Asha Kapoor",
            "practice_name": "Sunrise Nutrition Studio",
            "mobile": "+919820000000",
            "accepted_terms_version": TERMS,
        },
    )

    print("Activating owner (simulating the email-verification click)…", flush=True)
    sql_superuser("UPDATE users SET status = 'active' WHERE email = '%s'" % OWNER_EMAIL)

    print("Signing in…", flush=True)
    tokens = call(
        "POST",
        "/api/v1/public/auth/login",
        body={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
    )
    token = tokens["access_token"]

    print("Creating clients…", flush=True)
    created: list[dict] = []
    for name, mobile, email, stage, sex, city, diet, source in CLIENTS:
        client = call(
            "POST",
            "/api/v1/app/clients",
            token=token,
            body={
                "full_name": name,
                "mobile": mobile,
                "email": email,
                "stage": stage,
                "sex": sex,
                "city": city,
                "dietary_class": diet,
                "source": source,
            },
        )
        created.append(client)
        print(f"  + {name} ({stage})", flush=True)

    print("Creating a draft nutrition plan…", flush=True)
    target = created[0]
    plan = call(
        "POST",
        f"/api/v1/app/clients/{target['id']}/plans",
        token=token,
        body={
            "title": "Fat-loss reset — Week 1",
            "goal_type": "fat_loss",
            "day_count": 1,
            "target_energy_kcal": "1600",
            "target_protein_g": "90",
            "target_carbs_g": "160",
            "target_fat_g": "55",
        },
    )
    print(f"  plan for {target['full_name']}: version {plan['draft_version']['id']}", flush=True)

    print("\nSeed complete.")
    print(f"  Login: {OWNER_EMAIL} / {OWNER_PASSWORD}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        raise

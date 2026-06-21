# PSB / Hinata — Toast Group (cloud routine support)

Minimal code + template that a **scheduled cloud agent** uses to regenerate the PITCH Sports Bar
"Weekly Ops" brief from live data every Monday.

## What's here (and what's deliberately NOT)
- `Finance/toast_client.py` — Toast auth + token cache. Reads creds from **environment variables**
  in the cloud (no local vault present), or `.vault/toast.env` when run locally.
- `Finance/toast_catering.py` — combined PITCH + Hinata catering pull from the Toast Orders API.
- `templates/weekly-ops-template.html` — de-identified 6-tab brief skeleton (styling + structure only).

**NOT in this repo, on purpose:** no secrets, no employee PII (phone numbers), no customer data.
Those stay on the local machine. Generated briefs (which DO contain customer names) are git-ignored.

## Required environment variables (set as CLOUD ENVIRONMENT SECRETS — never commit)
See `.env.example`. The cloud environment must provide:
`TOAST_CLIENT_ID`, `TOAST_CLIENT_SECRET` (Analytics) ·
`TOAST_STD_CLIENT_ID`, `TOAST_STD_CLIENT_SECRET` (Standard/Orders) ·
`TOAST_PITCH_GUID`, `TOAST_HINATA_GUID`, `TOAST_API_HOST`.

## What the routine does (driven by its prompt, not this repo)
1. `python3 Finance/toast_catering.py 9` → next week's catering (both restaurants)
2. Google Calendar (connector) → sports + Sling roster
3. Gmail (connector) → "Upcoming Toast Bookings" email → covers
4. Toast Analytics + Orders APIs → net sales + lean top-sellers
5. Generate the 6-tab HTML from `templates/weekly-ops-template.html`
6. Email the finished brief to the team

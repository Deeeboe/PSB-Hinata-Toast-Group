# Notes & Feature Ideas

## SMS approval workflow for delivery drivers (requested 2026-06-22)

**Goal:** Extend the same approval process we send to department heads so that
individual delivery drivers can confirm a day's job by text message.

**How it should work:**
- When a job is needed for the day, send an **individual SMS** to the relevant driver.
- Driver replies **YES** or **NO** to accept/decline that specific delivery for the day.
- Covers three job types: **Deliveries**, **Rentals**, and **Catering**.
- Mirrors the existing department-head approval flow (same structure, but per-driver
  and routed to drivers instead of department heads).

**First-to-accept-wins (claim lock):**
- A job can be offered to **multiple drivers at once**, but only the **first YES** claims it.
- The moment one driver replies YES, that job is **locked/assigned** to them.
- Any driver who replies YES **after** the job is already claimed gets an automatic
  reply like: *"Sorry, [name] already accepted this delivery before you — it's been
  assigned."*
- Need to handle the race condition carefully (near-simultaneous replies): the system
  must pick a single definitive winner and reject all later replies, so a job is never
  double-assigned.
- Optional later: notify the rest of the drivers that the job has been filled so they
  don't bother replying.

**Open questions / to figure out later:**
- SMS provider/channel (e.g., Twilio, Zapier SMS, or existing department-head approval system).
- How driver phone numbers are stored (kept out of this repo — no PII committed here).
- How a YES/NO reply is captured and fed back into the schedule/assignment.
- Whether this becomes its own reusable skill.

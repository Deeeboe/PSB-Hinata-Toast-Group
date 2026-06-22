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

**Open questions / to figure out later:**
- SMS provider/channel (e.g., Twilio, Zapier SMS, or existing department-head approval system).
- How driver phone numbers are stored (kept out of this repo — no PII committed here).
- How a YES/NO reply is captured and fed back into the schedule/assignment.
- Whether this becomes its own reusable skill.

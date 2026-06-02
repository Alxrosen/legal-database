# Avvo — data source notes

Host: `https://www.avvo.com`

Status: **RECON COMPLETE 2026-06-02 — BLOCKED. Needs a decision before
any scraper is built.**

## The blocker: hard Cloudflare challenge

Every directory path returns a `403 "Just a moment..."` Cloudflare JS
challenge, and — unlike Justia — a **full browser header set does NOT
get through**. Tested 2026-06-02 with the same realistic Chrome header
set that unlocks Justia:

| URL | result |
|---|---|
| `https://www.avvo.com/` | 403 Cloudflare challenge |
| `/find-a-lawyer` | 403 Cloudflare challenge |
| `/all-lawyers/az/phoenix.html` | 403 Cloudflare challenge |
| `/personal-injury-lawyer/az/phoenix.html` | 403 Cloudflare challenge |

This is a real JS/JA3-level challenge (not header shape). Plain `httpx`
cannot pass it. Getting in would require one of:

1. **TLS-impersonation** (`curl_cffi` / `tls-client`) to present a real
   Chrome JA3 fingerprint. Often enough for managed challenges. New
   dependency; per project rules we hold off on impersonation tooling
   without explicit sign-off.
2. **Headless browser** (Playwright/Selenium) to execute the challenge
   JS and obtain the `cf_clearance` cookie, then hand it to `httpx`.
   Heavy dependency; slow; still a form of defeating the challenge.
3. **A challenge-solver service** — out of scope / cost.

All three are "defeating a Cloudflare challenge," which the project
posture (and the user's standing instruction) says **must not be
started without consulting the user first.**

## robots.txt (for the record)

`www.avvo.com/robots.txt` opens with ASCII art (a "go away" signal) but
the actual rules under `User-agent: *` allow the directory grain
(`/all-lawyers/`, `/{practice}-lawyer/...`) — the Disallows target
review/edit/search/account subpaths. Several bots are fully banned
(`GPTBot`, `Yandex`, `GoogleOther`, `voltron`, `008`). So robots is not
the blocker; Cloudflare is.

## Recommendation

Hold. Justia (built) covers the additional-coverage goal for now. If
Avvo is wanted, decide between option 1 (curl_cffi, lightest) and
option 2 (Playwright, heaviest but most reliable) — then build a scraper
whose fetch layer uses that transport while the parser/pipeline follow
the same pattern as the other sources.

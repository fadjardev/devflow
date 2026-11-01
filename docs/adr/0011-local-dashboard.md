# 11. A local web dashboard beside Telegram

Date: 2026-11-01 · Status: accepted

## Context

Telegram is good away from the desk and poor at the desk: long specs, diffs,
usage over a month and logs are hard to read on a phone. Some users don't
want a Telegram bot at all.

## Decision

A dashboard inside the devflow package (Starlette, Jinja2 and a vendored
htmx; no build step and no CDN), driving the same `Flow` as the bot. A new
`runtime.py` owns the Flow, its background loops and a fan-out notifier, so
the bot, the dashboard, or both in one event loop get every notification.
`devflow dashboard` runs it without Telegram.

Every action calls the same `Flow` method a Telegram command does: the
dashboard adds no way past a gate that the bot doesn't have.

Security, since any web page you visit can send requests to 127.0.0.1:

- it binds to 127.0.0.1 only, never another interface;
- the `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`
  (DNS rebinding);
- a random token in `runtime/dashboard.token` (owner-only) logs you in via a
  printed link and becomes an HttpOnly, SameSite=Strict cookie;
- every POST needs a CSRF token derived from it, and a foreign `Origin` is
  refused;
- agent and note text is rendered as Markdown with raw HTML off, after
  secret masking.

Live updates poll small fragments every few seconds instead of using
server-sent events: it is one local user, and polling survives restarts and
proxies without extra code.

## Consequences

- `starlette`, `uvicorn` and `markdown-it-py` were already installed through
  `mcp`; `jinja2` is the one new dependency.
- `/ask` conversations in the dashboard live in memory and end at a restart,
  like the bot's reply-to-follow-up memory.
- Anyone with your user account can read the token file, as they could read
  the vault and the bot token: the boundary is the OS user.

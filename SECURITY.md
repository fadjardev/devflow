# Security policy

devflow runs AI agents unattended on your machine, inside git checkouts that
often hold credentials. Security bugs here can mean leaked secrets or code
running where it shouldn't, so please report them privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's
**Security** tab and click **Report a vulnerability**. Please don't open a
public issue, pull request or discussion for a security problem.

Include what you ran, what you expected, what happened, and the smallest setup
that reproduces it. Remove tokens, secrets and private code from anything you
attach.

What to expect:

- an acknowledgement within 7 days;
- an assessment, and a plan or a reason for declining, within 30 days;
- a fix released and the advisory published within 90 days of the report,
  or earlier once a fix is out. If a fix needs longer, we will agree on a new
  date with you.

You will be credited in the advisory unless you prefer not to be.

## What counts

Anything that lets an agent, a repository's content, or another Telegram user
get past the safety model in the [README](README.md#safety-model). Examples:

- an agent writing outside its worktree, reading a `DENY_READ` file, pushing,
  or running git that devflow should refuse;
- an agent moving a task past a human gate, or writing task or project notes it
  shouldn't;
- a Telegram user or chat other than the configured one getting a reply or
  triggering an action;
- secrets reaching logs or Telegram messages.

The README lists gaps that are **already known** (for example, the opencode
worker's shell is not sandboxed). Reports that show a new way to exploit a
known gap are still welcome.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.2.x | yes |
| 0.1 (untagged, before the security hardening) | no |

Only the latest minor release gets security fixes.

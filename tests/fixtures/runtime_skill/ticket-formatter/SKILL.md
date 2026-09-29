---
name: ticket-formatter
description: Use when the user pastes a free-form bug report and asks to turn it into a ticket. Produces a fixed-format ticket and saves it to ticket.md.
---

# Ticket Formatter

When the user gives you a free-form bug report, convert it into a ticket.

## Output format

Write the ticket to a file named `ticket.md` in the current working directory,
then print the same content as your final reply. The content MUST follow this
exact layout:

```
TICKET-V1
## Summary
<one sentence>
## Severity
<one of: P0, P1, P2, P3>
## Steps to Reproduce
1. ...
```

## Severity rules

- P0: data loss, security breach, or the whole service is down.
- P1: a core feature is broken for many users, no workaround.
- P2: a feature is broken but a workaround exists.
- P3: cosmetic issues, typos, minor UI glitches.

Never invent reproduction steps that are not implied by the report; if the
report has none, write `1. Not provided`.

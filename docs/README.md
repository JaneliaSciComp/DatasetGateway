---
doc_status: living
sync_policy: Update this index whenever docs are added, moved, or reclassified.
last_reviewed: 2026-06-01
---

# Documentation

This directory separates current operational documentation from design history.
Use the front matter at the top of each Markdown file to tell whether a document
is expected to track code changes.

## Status Markers

- `living`: Current user/admin/project documentation. Update in the same change
  when behavior, setup, commands, or supported workflows change.
- `living-reference`: Current API or integration reference. Update when endpoint
  contracts, auth behavior, or integration assumptions change.
- `historical-design`: Design context kept for background. Do not treat as the
  canonical description of current behavior unless it has been explicitly
  refreshed.
- `historical-record`: Implementation history. Preserve as a record; update only
  for deliberate retrospective notes.
- `brainstorm`: Pre-decision proposal or design exploration under review. Not
  synchronized with code and not authoritative; expected to be superseded or
  split into living docs once a direction is chosen.

## Living Docs

- [User manual](user-manual.md) - end-user workflows, roles, login, TOS, and API use.
- [Admin manual](admin-manual.md) - setup, Django admin, environment variables,
  and management commands.
- [CAVE auth endpoints](cave-auth-endpoints.md) - current CAVE compatibility and
  SCIM provisioning reference.
- [Clio support](clio-support.md) - current Clio integration behavior and migration
  notes.
- [Service accounts](service-accounts.md) - current non-human identity model,
  token behavior, and web/admin workflows.
- [Backups & restore](backups.md) - encrypted tiered SQLite backups to nearline,
  admin keypair setup, and the tested restore runbook.

## Proposals (under review)

- [ngauth brainstorming](design/ngauth-brainstorming.md) - pre-decision brainstorm on
  ngauth support, operational parity with `tos-ngauth`, and multi-project /
  multi-bucket credential design. `doc_status: brainstorm` - not synced to code;
  supersede or split into living docs once a direction is chosen.

## Design Archive

- [Architecture](design/architecture.md) - historical architecture design. Useful
  for intent and concepts, but not the source of truth for current code.
- [Implementation record](design/implemented-plan.md) - historical build plan and
  retrospective implementation notes.

When in doubt, trust the code and tests first, then update the affected living
docs so the next reader does not need to rediscover the same distinction.

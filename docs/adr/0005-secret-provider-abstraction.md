# ADR-0005: Secret-provider abstraction; control plane stores only secret references

## Status

Accepted

## Context

Customer database credentials must be resolvable to actually connect, but must never be
recoverable from the Hermes-RPT control-plane database, its APIs, or its logs. Different
deployment environments will want different secret backends (local dev, Azure Key Vault, Google
Secret Manager) without changing calling code.

## Decision

Introduce a `SecretProvider` interface (`resolve(reference) -> credential`,
scoped lifetime, no persistence of the resolved value beyond an in-memory connection setup). The
control plane stores only an opaque `DatabaseCredentialReference`, never a resolved secret. A
local-development provider is implemented first; Azure Key Vault and Google Secret Manager
adapters are specified as interfaces in Phase 4 but are not required to be functional until a
concrete deployment needs them. No API ever returns a resolved secret value; connection status
endpoints return safe metadata only (e.g. "reachable: true", never the credential).

## Consequences

* Swapping secret backends per environment is a configuration change, not a code change, once a
  real cloud adapter is implemented.
* Local development necessarily uses a less secure provider; this is acceptable only because
  local development never touches real customer data (enforced by using synthetic Tenant Alpha/
  Beta fixtures throughout).
* Adds one layer of indirection to every connection setup; accepted given the sensitivity of the
  underlying secret.

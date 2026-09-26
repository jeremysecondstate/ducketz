# Duckets

## Duckets Law

Every file, class, function, method, constant, setting, dependency, and UI
element must have a reason to exist today.

Code is only allowed if it is directly used by the current application:

- no placeholder modules;
- no speculative abstractions;
- no unused helpers;
- no copied legacy code;
- no “we might need this later.”

Before committing, every new symbol must answer:

1. What uses this today?
2. What breaks if this is deleted?
3. Is this simpler than the alternative?

If the answer is unclear, delete it.

## System documentation

- [Hyperliquid portfolio system and H.Y.P.E.R. operations](docs/hyperliquid-system-analysis/README.md)
- [Powder readiness, user activation and recovery](docs/hyperliquid-system-analysis/POWDER_ACTIVATION.md)
- [Stock/options Loops system analysis](docs/loops-system-analysis/README.md)

Keep the Hyperliquid reference aligned with implementation changes using its
[maintenance checklist](docs/hyperliquid-system-analysis/MAINTENANCE.md).

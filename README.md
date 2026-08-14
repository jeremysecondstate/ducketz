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
---
inclusion: always
---

# Context7 Documentation Usage

Use the Context7 MCP server to pull up-to-date, version-accurate documentation and code examples before writing or changing code that relies on external libraries or frameworks.

## When to Consult Context7

- Adding, upgrading, or configuring a dependency (e.g. `adafruit-circuitpython-servokit`, `pyaudio`, `numpy`, Flask).
- Verifying library compatibility and current API signatures before implementation.
- Working through Specs (requirements, design, tasks) or Agents that depend on external library behavior.
- Resolving errors that may stem from outdated or deprecated API usage.

## How to Use It

- Resolve the library ID first, then query the docs scoped to a single concept per call.
- Prefer official documentation from Context7 over assumptions from training data, especially for versioned APIs.
- Confirm the version in use (see `requirements.txt`) and query docs for that version when possible.
- Treat fetched documentation as reference only; do not paste large verbatim excerpts into the codebase.

## Precedence

- If Context7 documentation conflicts with existing project conventions, follow the project's established patterns and flag the discrepancy rather than silently changing behavior.

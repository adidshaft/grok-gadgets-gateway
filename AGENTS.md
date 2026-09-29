# Gateway contributor instructions

Own only this repository. Canonical protocol changes require coordination with the Linux and ESP32 SDKs. Separate assistant tools, domain, device transport and test controls. Never report a device acknowledgement as physical verification.

Use short-lived feature branches and small coherent commits referencing a local issue ID. Before integration run `uv run pytest` and `uv run ruff check .`; update planning/issues.json with evidence. Do not publish, push, deploy, make paid API calls or modify community services without explicit authorization. Use Chrome or built-in browser, never Brave/Safari/Passwords. Secrets stay outside Git.

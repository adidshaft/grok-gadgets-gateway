# Gateway contributor instructions

Own only this repository. Canonical protocol changes require coordination with the Linux and ESP32 SDKs. Separate assistant tools, domain, device transport and test controls. Never report a device acknowledgement as physical verification.

Use short-lived feature branches and small coherent commits referencing a local issue ID. Before integration run `uv run pytest` and `uv run ruff check .`; update planning/issues.json with evidence. Do not publish, push, deploy, make paid API calls or modify community services without explicit authorization. Use Chrome or built-in browser, never Brave/Safari/Passwords. Secrets stay outside Git.

Use bounded subagents with explicit file ownership when helpful. Any research subagent,
including platform or evidence research, must use a current GPT-6.1/GPT-6 model at Max
reasoning. If unavailable, report it before substituting. Ordinary implementation/review
may use established settings. Do not research on a lower-effort assignment; ask the
coordinator to arrange an authorized research task.

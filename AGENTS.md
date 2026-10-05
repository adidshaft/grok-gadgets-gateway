# Gateway contributor instructions

Own only this repository. Canonical protocol changes require coordination with the Linux and ESP32 SDKs. Separate assistant tools, domain, device transport and test controls. Never report a device acknowledgement as physical verification.

Use short-lived feature branches and small coherent commits referencing a local issue ID. Before integration run `uv run pytest` and `uv run ruff check .`; update planning/issues.json with evidence. Do not publish, push, deploy, make paid API calls or modify community services without explicit authorization. Use Chrome or built-in browser, never Brave/Safari/Passwords. Secrets stay outside Git.

Use bounded subagents with explicit file ownership when helpful. Research tasks use the strongest available reasoning and cite primary sources. If unavailable, report it before substituting. Ordinary implementation/review
may use established settings. Do not research on a lower-effort assignment; ask the
coordinator to arrange an authorized research task.

## Ignore rules and publication privacy

Keep `.gitignore` current whenever a new tool produces caches, build output, local device configurations, execution logs or credentials. Preserve reviewed sample configuration files and the hub's verified public simulator download. Check new patterns with `git check-ignore`, then review the staged file list before committing. Ignore rules do not remove tracked files or past history; never merge the private pre-publication history back into a public branch. Use the sanitized public checkout and a public or GitHub noreply commit email.

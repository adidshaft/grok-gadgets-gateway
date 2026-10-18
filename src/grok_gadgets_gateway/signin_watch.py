"""`oauth watch` (hub ADR 014 §4, the #211 residual): did Grok Bot's sign-in survive each hour?

Grok Bot's access token lasts 1 hour; its refresh token keeps the sign-in alive. This reads the
request log and counts, for each UTC day: fresh sign-ins, refreshes, expired access tokens,
refused refreshes by reason, 401 answers and remote tool calls. It also lists each expiry that
was followed by a 401 before the next refresh for that sign-in, which is how the two
unexplained 401s of 17 October 2026 looked. It shows token names and counts only: never a
token, a client name, an address or an argument.
"""

from collections import Counter, defaultdict
from datetime import datetime

from .report import _records

FIELDS = ("sign_ins", "refreshes", "expired", "unauthorized", "remote_calls")


def _when(record):
    return datetime.fromisoformat(record["ts"])


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def watch(paths, since=None):
    days = defaultdict(lambda: {**dict.fromkeys(FIELDS, 0), "refresh_rejected": Counter()})
    waiting = {}  # token name -> when its access token expired, until a refresh follows
    expiries = refreshed = 0
    failures = []
    for record in _records(paths):
        day = record["ts"][:10]
        if since and day < since:
            continue
        event, name = record.get("event"), record.get("token")
        counts = days[day]
        if event == "oauth_token_issued":
            counts["sign_ins"] += 1
            waiting.pop(name, None)
        elif event == "oauth_token_refreshed":
            counts["refreshes"] += 1
            if waiting.pop(name, None) is not None:
                refreshed += 1
        elif event == "oauth_access_expired":
            counts["expired"] += 1
            expiries += 1
            waiting[name] = {"expired": _when(record), "reported": False}
        elif event == "oauth_refresh_rejected":
            counts["refresh_rejected"][record.get("reason") or "unknown"] += 1
        elif event == "http_rejected" and record.get("status") == 401:
            counts["unauthorized"] += 1
            for token, open_expiry in waiting.items():
                if not open_expiry["reported"]:
                    open_expiry["reported"] = True
                    failures.append(
                        {
                            "token": token,
                            "expired": _stamp(open_expiry["expired"]),
                            "first_401": _stamp(_when(record)),
                        }
                    )
        elif event == "mcp_tool_call" and record.get("remote"):
            counts["remote_calls"] += 1
    rows = [
        {
            "day": day,
            **{k: v for k, v in values.items() if k != "refresh_rejected"},
            "refresh_rejected": dict(values["refresh_rejected"]),
        }
        for day, values in sorted(days.items())
    ]
    return {
        "days": rows,
        "sign_ins": sum(row["sign_ins"] for row in rows),
        "refreshes": sum(row["refreshes"] for row in rows),
        "expiries": expiries,
        "refreshed_after_expiry": refreshed,
        "expiry_then_401": failures,
    }


def format_watch(result):
    lines = []
    for row in result["days"]:
        rejected = ", ".join(f"{k} {v}" for k, v in sorted(row["refresh_rejected"].items()))
        lines.append(
            f"{row['day']}  fresh sign-ins {row['sign_ins']}  refreshes {row['refreshes']}  "
            f"expired {row['expired']}  401s {row['unauthorized']}  "
            f"remote calls {row['remote_calls']}"
            + (f"  refused refreshes: {rejected}" if rejected else "")
        )
    lines.append(
        f"total: fresh sign-ins {result['sign_ins']}, refreshes {result['refreshes']}, "
        f"expiries {result['expiries']} ({result['refreshed_after_expiry']} refreshed after)"
    )
    if result["expiry_then_401"]:
        for item in result["expiry_then_401"]:
            lines.append(
                f"401 after an expiry with no refresh between: {item['token']} expired "
                f"{item['expired']}, first 401 {item['first_401']}"
            )
    else:
        lines.append("no 401 between an expiry and its refresh")
    return lines

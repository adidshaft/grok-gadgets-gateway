"""The sign-in pages (GW-UX-001 #207, GW-UX-003, hub ADR 014 §3).

A person reaches these pages from Grok Bot's sign-in sheet, often on a phone. The code page
has one instruction, one field and one button; tips, recovery codes and the operator command
sit under Help. The app's self-declared name is shown once, quietly, because any app can
choose any name. Everything is inline: no script, no external file, no font download. The
page policy stays `default-src 'none'` with inline styles only, and it has no `form-action`,
because browsers apply that to the redirect back to Grok Bot after a right code (GW-CLAIM-006).
"""

import html

APPROVE_PATH = "/oauth/approve"
NO_STORE = {"Cache-Control": "no-store"}
HEADERS = {
    **NO_STORE,
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Referrer-Policy": "no-referrer",
}

# The website's palette and type: white paper, near-black ink, Helvetica, light weights.
_STYLE = """
:root{--paper:#fff;--ink:#111;--muted:#626262;--line:#e6e6e6;--field:#f7f7f7;--alert:#9b1c1c}
*{box-sizing:border-box}
html{color-scheme:light}
body{margin:0;background:var(--paper);color:var(--ink);
font:17px/1.5 "Helvetica Neue",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;
-webkit-text-size-adjust:100%}
main{max-width:24rem;margin:0 auto;padding:24px 20px 40px}
.brand{display:flex;align-items:center;gap:9px;font-size:15px;margin:0 0 48px}
.brand svg{width:26px;height:26px;flex:none}
h1{font-size:34px;font-weight:500;letter-spacing:-.045em;line-height:1.08;margin:0 0 10px}
p{margin:0 0 14px}
.muted{color:var(--muted);font-size:15px}
form{margin:30px 0 28px}
input[name=code]{display:block;width:100%;height:64px;border:1px solid var(--line);
border-radius:14px;background:var(--field);color:var(--ink);padding:0 14px;text-align:center;
font:500 26px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.12em;
text-transform:uppercase;-webkit-appearance:none;appearance:none}
input[name=code]:focus{outline:2px solid var(--ink);outline-offset:2px;background:var(--paper)}
input[name=code]::placeholder{color:#c4c4c4}
input[aria-invalid=true]{border-color:var(--alert)}
button{display:block;width:100%;height:54px;margin:12px 0 0;border:0;border-radius:999px;
background:var(--ink);color:var(--paper);font-family:inherit;font-size:17px;cursor:pointer}
.alert{color:var(--alert);font-size:15px;margin:0 0 12px}
details{border-top:1px solid var(--line);padding-top:14px;color:var(--muted);font-size:15px}
summary{cursor:pointer;color:var(--ink)}
details p{margin:10px 0 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--field);border-radius:10px;
padding:12px 14px;margin:10px 0 0;color:var(--ink);font:14px/1.45 ui-monospace,Menlo,monospace}
main>pre{margin:22px 0 22px;font-size:15px}
"""

# The Grok Bot face: a white round face with two tilted eyes (the project's mark).
_MARK = (
    '<svg viewBox="0 0 512 512" role="img" aria-label="Grok Bot">'
    '<rect width="512" height="512" rx="120" fill="#090909"/>'
    '<circle cx="256" cy="256" r="210" fill="#fff"/>'
    '<g fill="#191919" transform="rotate(28 256 190)">'
    '<rect x="172" y="93" width="42" height="110" rx="21"/>'
    '<rect x="271" y="101" width="54" height="126" rx="27"/></g></svg>'
)


def _shell(body, refresh=False):
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{meta}<title>Grok Gadgets sign-in</title><style>{_STYLE}</style></head>"
        f'<body><main><p class="brand">{_MARK}<span>Grok Gadgets</span></p>{body}'
        "</main></body></html>"
    )


def _who(client_name):
    """The name the signing-in app gives itself, once and quietly. Any app can choose any name."""
    if not client_name:
        return ""
    return f'<p class="muted">“{html.escape(client_name)}” wants to use your gadgets.</p>'


def _help(*paragraphs):
    return (
        "<details><summary>Help</summary>"
        + "".join(f"<p>{p}</p>" for p in paragraphs)
        + ("</details>")
    )


def code_page(request_id, client_name, operator_code, error=None, tries_left=None):
    """Accounts mode: the person types the code their gadget shows, or a recovery code."""
    alert = invalid = ""
    if error == "wrong":
        left = ""
        if tries_left is not None:
            left = f" {tries_left} {'try' if tries_left == 1 else 'tries'} left."
        alert = (
            '<p class="alert" role="alert">That code did not work. '
            f"Codes change every 10 minutes.{left}</p>"
        )
        invalid = ' aria-invalid="true"'
    return _shell(
        "<h1>Type the code on your gadget’s screen</h1>"
        f"{_who(client_name)}"
        f'<form method="post" action="{APPROVE_PATH}">{alert}'
        f'<input type="hidden" name="request" value="{html.escape(request_id)}">'
        '<input id="code" name="code" aria-label="Code" placeholder="XXXX–XXXX" maxlength="24" '
        'autocomplete="one-time-code" autocapitalize="characters" autocorrect="off" '
        f'spellcheck="false" inputmode="text"{invalid} autofocus required>'
        '<button type="submit">Connect</button></form>'
        + _help(
            "Codes change every 10 minutes.",
            "No code on the screen? The gadget may belong to someone already; ask them to "
            "share it.",
            "Have a recovery code from an earlier sign-in? Type it instead.",
            "Any app can choose the name it shows here. This page never asks for a password.",
            "Relay operator: approve on the relay host, then press Connect with the field "
            f"empty.<pre>grok-gadgets-gateway oauth approve {html.escape(operator_code)} "
            "--name NAME</pre>",
        )
    )


def waiting_page(client_name, code):
    """Single-operator mode: the owner approves on the gateway's computer; this page waits."""
    return _shell(
        "<h1>Approve on your gateway</h1>"
        f"{_who(client_name)}"
        f"<pre>grok-gadgets-gateway oauth approve {html.escape(code)} --name grok-bot</pre>"
        '<p class="muted">This page continues by itself.</p>'
        + _help(
            "Run the command on the computer that runs the gateway.",
            "If you did not start this sign-in, do nothing; it expires in 10 minutes.",
            "Any app can choose the name it shows here.",
        ),
        refresh=True,
    )


_ENDED = {
    "expired": "This sign-in has expired.",
    "limited": "Too many tries.",
}


def ended_page(reason):
    """A sign-in that cannot go on: expired, unknown or too many wrong codes."""
    return _shell(
        f"<h1>{_ENDED.get(reason, _ENDED['expired'])}</h1>"
        "<p>Start the sign-in again from Grok Bot.</p>"
    )

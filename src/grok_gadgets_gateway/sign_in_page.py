"""The sign-in pages (GW-UX-001 #207, hub ADR 014 §3).

A person reaches these pages from Grok Bot's sign-in sheet, often on a phone. They show one
field for the code on the gadget's screen, a drawing of where to find it, and plain errors.
Everything is inline: no script, no external file, no font download. The page policy stays
`default-src 'none'` with inline styles only, and it has no `form-action`, because browsers
apply that to the redirect back to Grok Bot after a right code (GW-CLAIM-006).
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
body{margin:0;background:var(--paper);color:var(--ink);
font:17px/1.55 "Helvetica Neue",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;
-webkit-text-size-adjust:100%}
main{max-width:24rem;margin:0 auto;padding:28px 20px 40px}
.brand{display:flex;align-items:center;gap:10px;font-size:15px;margin:0 0 34px}
.brand svg{width:28px;height:28px;flex:none}
h1{font-size:40px;font-weight:500;letter-spacing:-.05em;line-height:1.02;margin:0 0 12px}
p{margin:0 0 14px}
.muted{color:var(--muted);font-size:15px}
.gadget{display:block;width:100%;max-width:240px;height:auto;margin:26px auto}
input[name=code]{width:100%;height:60px;border:1px solid var(--line);border-radius:12px;
background:var(--field);color:var(--ink);padding:0 14px;text-align:center;
font:500 26px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.12em;
text-transform:uppercase}
input[name=code]:focus{outline:2px solid var(--ink);outline-offset:2px}
input[name=code]::placeholder{color:#b5b5b5}
button{width:100%;height:54px;margin:12px 0 22px;border:0;border-radius:999px;
background:var(--ink);color:var(--paper);font-family:inherit;font-size:17px;cursor:pointer}
.alert{color:var(--alert);font-size:15px}
.big{font:500 30px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.08em;
margin:8px 0 18px}
details{border-top:1px solid var(--line);padding-top:12px;color:var(--muted);font-size:15px}
summary{cursor:pointer;color:var(--ink)}
details p{margin:10px 0 0}
main>pre{margin:0 0 22px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--field);border-radius:10px;
padding:10px 12px;margin:10px 0 0;color:var(--ink);font:14px/1.45 ui-monospace,Menlo,monospace}
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

# A gadget with its screen showing a code, so the person knows where to look.
_GADGET = (
    '<svg class="gadget" viewBox="0 0 300 150" role="img" '
    'aria-label="A gadget screen showing Code XXXX–XXXX">'
    '<rect x="6" y="6" width="288" height="138" rx="22" fill="none" '
    'stroke="currentColor" stroke-width="3"/>'
    '<rect x="30" y="26" width="240" height="98" rx="10" fill="#111"/>'
    '<text x="150" y="62" fill="#fff" font-family="ui-monospace,Menlo,monospace" '
    'font-size="18" text-anchor="middle">Code</text>'
    '<text x="150" y="100" fill="#fff" font-family="ui-monospace,Menlo,monospace" '
    'font-size="30" font-weight="700" text-anchor="middle" letter-spacing="3">XXXX–XXXX</text>'
    "</svg>"
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
    """The name the signing-in app gives itself. Any app can choose any name."""
    if not client_name:
        return ""
    return f'<p class="muted">Requested by “{html.escape(client_name)}”</p>'


def _help(*paragraphs):
    return (
        "<details><summary>Help</summary>"
        + "".join(f"<p>{p}</p>" for p in paragraphs)
        + ("</details>")
    )


def code_page(request_id, client_name, operator_code, error=None, tries_left=None):
    """Accounts mode: the person types the code their gadget shows, or a recovery code."""
    alert = ""
    if error == "wrong":
        left = ""
        if tries_left is not None:
            left = f" {tries_left} {'try' if tries_left == 1 else 'tries'} left."
        alert = (
            '<p class="alert" role="alert">That code did not work. '
            f"Codes change every 10 minutes.{left}</p>"
        )
    return _shell(
        "<h1>Connect Grok Bot</h1>"
        "<p>Type the code on your gadget's screen.</p>"
        f"{_who(client_name)}{alert}"
        f"{_GADGET}"
        f'<form method="post" action="{APPROVE_PATH}">'
        f'<input type="hidden" name="request" value="{html.escape(request_id)}">'
        '<input id="code" name="code" aria-label="Code" placeholder="XXXX–XXXX" maxlength="24" '
        'autocomplete="one-time-code" autocapitalize="characters" autocorrect="off" '
        'spellcheck="false" inputmode="text" autofocus required>'
        '<button type="submit">Connect</button></form>'
        + _help(
            "Codes change every 10 minutes.",
            "No code on the screen? The gadget may belong to someone already; ask them to "
            "share it.",
            "Have a recovery code from an earlier sign-in? Type it instead.",
            "Any app can choose the name shown above. This page never asks for a password.",
            "Relay operator: approve on the relay host, then press Connect with the field "
            f"empty.<pre>grok-gadgets-gateway oauth approve {html.escape(operator_code)} "
            "--name NAME</pre>",
        )
    )


def waiting_page(client_name, code):
    """Single-operator mode: the owner approves on the gateway's computer; this page waits."""
    code = html.escape(code)
    return _shell(
        "<h1>Approve on your gateway</h1>"
        f'<p class="big">{code}</p>'
        f"{_who(client_name)}"
        f"<pre>grok-gadgets-gateway oauth approve {code} --name grok-bot</pre>"
        + _help(
            "Run the command on the computer that runs the gateway. This page continues by itself.",
            "If you did not start this sign-in, do nothing; it expires in 10 minutes.",
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

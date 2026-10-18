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

_STYLE = """
:root{--paper:#fff;--ink:#111;--muted:#5c5c5c;--line:#d9d9d9;--field:#f6f6f6;--alert:#9b1c1c;
--alert-paper:#fdf0f0;--button:#111;--button-ink:#fff;--screen:#0b0b0b;--screen-ink:#f2f2f2}
@media (prefers-color-scheme: dark){:root{--paper:#0e0e0e;--ink:#f2f2f2;--muted:#a8a8a8;
--line:#333;--field:#1a1a1a;--alert:#ffb4b4;--alert-paper:#2a1414;--button:#f2f2f2;
--button-ink:#111;--screen:#000;--screen-ink:#f2f2f2}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:17px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
-webkit-text-size-adjust:100%}
main{max-width:26rem;margin:0 auto;padding:28px 20px 40px}
.brand{display:flex;align-items:center;gap:10px;font-size:15px;color:var(--muted);margin:0 0 22px}
.brand svg{width:34px;height:34px;flex:none}
h1{font-size:27px;line-height:1.2;margin:0 0 10px;letter-spacing:-.01em}
p{margin:0 0 14px}
.who{color:var(--muted);font-size:15px}
.gadget{display:block;width:100%;max-width:300px;height:auto;margin:18px auto 20px}
label{display:block;font-weight:600;margin:0 0 8px}
input[name=code]{width:100%;height:64px;border:2px solid var(--line);border-radius:14px;
background:var(--field);color:var(--ink);padding:0 14px;text-align:center;
font:600 28px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.14em;
text-transform:uppercase}
input[name=code]:focus{outline:3px solid var(--ink);outline-offset:2px;border-color:var(--ink)}
input[name=code]::placeholder{color:var(--muted);opacity:.55}
button{width:100%;height:56px;margin:14px 0 18px;border:0;border-radius:14px;
background:var(--button);color:var(--button-ink);font-family:inherit;font-size:18px;
font-weight:600;cursor:pointer}
.hint{color:var(--muted);font-size:15px}
.alert{background:var(--alert-paper);color:var(--alert);border-radius:12px;padding:12px 14px;
font-weight:600}
.big{font:600 30px/1.2 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
letter-spacing:.1em;margin:6px 0 16px}
details{border-top:1px solid var(--line);padding-top:12px;color:var(--muted);font-size:14px}
summary{cursor:pointer}
code,pre{font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--field);border-radius:10px;
padding:10px 12px;margin:10px 0 0;color:var(--ink)}
footer{margin-top:26px;color:var(--muted);font-size:13px}
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
    '<rect x="30" y="26" width="240" height="98" rx="10" style="fill:var(--screen)"/>'
    '<text x="150" y="62" style="fill:var(--screen-ink)" font-family="ui-monospace,Menlo,monospace" '
    'font-size="18" text-anchor="middle">Code</text>'
    '<text x="150" y="100" style="fill:var(--screen-ink)" font-family="ui-monospace,Menlo,monospace" '
    'font-size="30" font-weight="700" text-anchor="middle" letter-spacing="3">XXXX–XXXX</text>'
    "</svg>"
)


def _shell(body, refresh=False):
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"{meta}<title>Grok Gadgets sign-in</title><style>{_STYLE}</style></head>"
        f'<body><main><p class="brand">{_MARK}<span>Grok Gadgets</span></p>{body}'
        "<footer>This page never asks for a password.</footer></main></body></html>"
    )


def _who(client_name):
    """The name the signing-in app gives itself. Any app can choose any name."""
    if not client_name:
        return ""
    return f'<p class="who">The app signing in calls itself {html.escape(client_name)}.</p>'


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
        "<h1>Connect Grok Bot to your gadget</h1>"
        f"{_who(client_name)}{alert}"
        "<p>Type the code on your gadget's screen.</p>"
        f"{_GADGET}"
        f'<form method="post" action="{APPROVE_PATH}">'
        f'<input type="hidden" name="request" value="{html.escape(request_id)}">'
        '<label for="code">Code</label>'
        '<input id="code" name="code" placeholder="XXXX–XXXX" maxlength="24" '
        'autocomplete="one-time-code" autocapitalize="characters" autocorrect="off" '
        'spellcheck="false" inputmode="text" autofocus required>'
        '<button type="submit">Connect</button></form>'
        '<p class="hint">Codes change every 10 minutes. No code on the screen? The gadget '
        "may belong to someone already; ask them to share it. Got a recovery code from an "
        "earlier sign-in? Type it here instead.</p>"
        "<details><summary>For the relay operator</summary>"
        "<p>Approve on the relay host, then press Connect with the field empty:</p>"
        f"<pre>grok-gadgets-gateway oauth approve {html.escape(operator_code)} --name NAME</pre>"
        "</details>"
    )


def waiting_page(client_name, code):
    """Single-operator mode: the owner approves on the gateway's computer; this page waits."""
    code = html.escape(code)
    return _shell(
        "<h1>Approve this sign-in</h1>"
        f"{_who(client_name)}"
        "<p>On the computer that runs the gateway, run:</p>"
        f"<pre>grok-gadgets-gateway oauth approve {code} --name grok-bot</pre>"
        f'<p class="big">Code {code}</p>'
        '<p class="hint">This page continues by itself. If you did not start this sign-in, '
        "do nothing; it expires in 10 minutes.</p>",
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

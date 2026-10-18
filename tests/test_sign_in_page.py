"""The sign-in page (GW-UX-001 #207, hub ADR 014 §3): clear on a phone, still strict."""

import re

import httpx2
import pytest
from test_claim_sign_in import REQUEST, accounts_server, code_client, device
from test_http import free_port
from test_oauth import list_devices

from grok_gadgets_gateway import sign_in_page as page
from grok_gadgets_gateway.accounts import REQUEST_WRONG_MAX

POLICY = "default-src 'none'; style-src 'unsafe-inline'"


def text_of(html):
    """Visible words, without tags, styles or the drawing."""
    html = re.sub(r"<style>.*?</style>|<svg.*?</svg>", " ", html, flags=re.DOTALL)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def code_page(**changes):
    values = {
        "request_id": "r" * 43,
        "client_name": "Grok Bot",
        "operator_code": "ABCD-EFGH",
    }
    values.update(changes)
    return page.code_page(**values)


def test_the_page_is_short_and_the_rest_is_behind_help():
    html = code_page()
    visible = text_of(html.split("<details>")[0])
    assert len(visible.split()) <= 20, visible


def test_the_code_page_explains_what_to_type():
    html = code_page()
    words = text_of(html)
    assert "Connect Grok Bot" in words
    assert "Requested by “Grok Bot”" in words
    assert "code on your gadget" in words.lower()
    assert "Codes change every 10 minutes" in words
    assert "recovery code" in words
    # The drawing shows a gadget screen with the code's shape, with an en dash so that
    # nobody (and no script) mistakes the example for a real code.
    assert "<svg" in html and "XXXX–XXXX" in html and "XXXX-XXXX" not in html
    # The Grok Bot face mark, inline.
    assert 'aria-label="Grok Bot"' in html


def test_the_code_field_is_phone_friendly():
    html = code_page()
    field = re.search(r"<input[^>]*name=\"code\"[^>]*>", html).group()
    for attribute in (
        'autocomplete="one-time-code"',
        'autocapitalize="characters"',
        'spellcheck="false"',
        'autocorrect="off"',
        'inputmode="text"',
        'placeholder="XXXX–XXXX"',
        'maxlength="24"',  # A recovery code has 19 characters.
        "autofocus",
    ):
        assert attribute in field
    assert 'method="post"' in html and f'action="{page.APPROVE_PATH}"' in html
    assert 'name="request" value="' + "r" * 43 + '"' in html


def test_errors_say_what_happened_and_how_many_tries_are_left():
    html = code_page(error="wrong", tries_left=3)
    words = text_of(html)
    assert "That code did not work. Codes change every 10 minutes." in words
    assert "3 tries left" in words
    assert "1 try left" in text_of(code_page(error="wrong", tries_left=1))
    assert 'role="alert"' in html


def test_the_operator_route_stays_but_out_of_the_way():
    html = code_page()
    assert "<details><summary>Help</summary>" in html
    assert "grok-gadgets-gateway oauth approve ABCD-EFGH --name NAME" in html


@pytest.mark.parametrize("typed", ["abcd-efgh", "ABCD–EFGH", "ABCD — EFGH", " abcd efgh "])
def test_codes_typed_or_pasted_on_a_phone_are_understood(typed):
    from grok_gadgets_gateway.accounts import normal_code

    assert normal_code(typed) == "ABCD-EFGH"


def test_names_and_ids_are_escaped():
    html = code_page(client_name='<script>alert("x")</script>', request_id='"><b>')
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert '"><b>' not in html


def test_a_client_without_a_name_gets_no_name_line():
    assert "Requested by" not in code_page(client_name=None)


def test_no_scripts_no_external_assets_and_a_mobile_viewport():
    for html in (
        code_page(),
        page.waiting_page(client_name="Grok Bot", code="ABCD-EFGH"),
        page.ended_page("expired"),
    ):
        assert "<script" not in html.lower()
        assert not re.search(r"(src|href)=\"(https?:)?//", html)
        assert "@import" not in html and "url(" not in html
        assert 'name="viewport" content="width=device-width, initial-scale=1"' in html
        assert "<title>Grok Gadgets sign-in</title>" in html


def test_the_operator_waiting_page_refreshes_and_shows_the_command():
    html = page.waiting_page(client_name="Grok Bot", code="ABCD-EFGH")
    assert 'http-equiv="refresh" content="3"' in html
    assert "grok-gadgets-gateway oauth approve ABCD-EFGH --name grok-bot" in html
    assert "expires in 10 minutes" in text_of(html)
    assert 'http-equiv="refresh"' not in code_page()


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("expired", "This sign-in has expired."),
        ("limited", "Too many tries."),
    ],
)
def test_ended_pages_send_the_person_back_to_grok_bot(reason, words):
    text = text_of(page.ended_page(reason))
    assert words in text
    assert "Start the sign-in again from Grok Bot." in text


def test_headers_keep_the_strict_policy():
    assert page.HEADERS["Content-Security-Policy"] == POLICY
    assert "form-action" not in page.HEADERS["Content-Security-Policy"]
    assert page.HEADERS["X-Frame-Options"] == "DENY"
    assert page.HEADERS["Cache-Control"] == "no-store"
    assert page.HEADERS["Referrer-Policy"] == "no-referrer"


# Through the relay


async def test_the_relay_serves_the_new_page_and_counts_down_tries(tmp_path):
    port, device_port = free_port(), free_port()
    async with accounts_server(tmp_path, port, device_port) as running:
        url = running.url.replace("127.0.0.1", "localhost")
        writer, reply, _ = await device(device_port)
        pages = []
        auth, _ = code_client(url, ["AAAA-AAAA", "BBBB-BBBB", reply["claim_code"]], pages)
        await list_devices(url, auth)
        writer.close()
    first, second, done = pages
    assert first.headers["content-security-policy"] == POLICY
    assert "Connect Grok Bot" in first.text
    assert f"{REQUEST_WRONG_MAX - 1} tries left" in text_of(first.text)
    assert f"{REQUEST_WRONG_MAX - 2} tries left" in text_of(second.text)
    assert "AAAA-AAAA" not in first.text and "BBBB-BBBB" not in second.text
    assert REQUEST.search(second.text)
    assert done.status_code == 302  # Straight back to Grok Bot after the right code.


async def test_an_unknown_request_gets_the_ended_page(tmp_path):
    port, device_port = free_port(), free_port()
    async with accounts_server(tmp_path, port, device_port), httpx2.AsyncClient() as web:
        gone = await web.get(f"http://localhost:{port}{page.APPROVE_PATH}?request=nope")
    assert gone.status_code == 400
    assert "Start the sign-in again from Grok Bot." in text_of(gone.text)
    assert gone.headers["content-security-policy"] == POLICY

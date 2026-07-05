"""Browser UI tests (headless Chromium via Playwright). Cover the landing
page flow, the app page rendering from fragment codes, the data:-URI
manifest injection and the localStorage persistence layers.

Skipped automatically when Playwright/Chromium is not installed:
    pip install -r requirements-dev.txt && python -m playwright install chromium
"""
import re

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("chromium not installed (python -m playwright install chromium)")
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.on("dialog", lambda d: d.accept())
    yield pg
    ctx.close()


def test_landing_create_channel_and_build_link(server, page):
    page.goto(server.base + "/")
    page.fill("#channel-name", "UI Channel")
    page.click("#create-btn")
    page.wait_for_selector("#create-result:not([hidden])")
    code = page.text_content("#new-code").strip()
    assert CODE_RE.fullmatch(code)

    # the created code was added to the link builder: URL + QR appear
    page.wait_for_selector("#link-result:not([hidden])")
    app_url = page.text_content("#app-url").strip()
    assert app_url.endswith("/a#codes=" + code)
    page.wait_for_selector("#qr svg")
    assert page.get_attribute("#open-app", "href") == app_url

    # a second (typed) code extends the link
    page.fill("#code-input", "a_second_code_0123456789")
    page.click("#add-code")
    app_url2 = page.text_content("#app-url").strip()
    assert app_url2.endswith(code + ",a_second_code_0123456789")

    # invalid codes are rejected with a message
    page.fill("#code-input", "nope")
    page.click("#add-code")
    assert "does not look like" in page.text_content("#add-error")


def test_landing_send_message_and_cookie_save(server, page):
    page.goto(server.base + "/")
    page.fill("#channel-name", "Send UI")
    page.click("#create-btn")
    page.wait_for_selector("#create-result:not([hidden])")
    code = page.text_content("#new-code").strip()

    # the send form and the developer curl example are prefilled with the code
    assert page.input_value("#send-code") == code
    assert page.text_content("#curl-code") == code

    # send a message straight from the landing page
    page.fill("#send-title", "Hello from landing")
    page.click("#send-btn")
    page.wait_for_selector("#send-ok:not([hidden])")
    assert "no device" in page.text_content("#send-ok").lower()  # sent=0, none yet
    # it really reached the channel
    snap = server.post("/api/messages", {"code": code}).json
    assert snap["messages"][0]["title"] == "Hello from landing"

    # cookie save is opt-in: the button does nothing until consent is ticked
    page.click("#save-btn")
    assert "tick the box" in page.text_content("#save-status").lower()
    page.check("#save-consent")
    page.click("#save-btn")
    assert "saved" in page.text_content("#save-status").lower()

    # returning to the page restores the channel from the cookie
    page.goto(server.base + "/")
    page.wait_for_selector("#code-list .codes-item span")
    assert code in page.text_content("#code-list")
    assert page.is_checked("#save-consent")

    # "forget" clears the cookie
    page.click("#forget-btn")
    page.goto(server.base + "/")
    assert page.query_selector("#code-list .codes-item") is None


def test_app_page_renders_channel_and_sends(server, page, channel):
    page.goto(server.base + "/a#codes=" + channel)
    page.wait_for_selector(".channel h2:has-text('Test Channel')")

    # data:-URI manifest carries the code in start_url
    href = page.get_attribute("#manifest-link", "href")
    assert href.startswith("data:application/manifest+json")
    assert channel in href

    # send a message through the UI (no push permission needed for sending)
    page.click(".channel summary")
    page.fill(".channel details input", "From the UI")
    page.click(".channel details button")
    page.wait_for_selector(".msg-title:has-text('From the UI')")

    # service worker registered on localhost (secure context)
    page.wait_for_function(
        "navigator.serviceWorker.getRegistrations().then(r => r.length > 0)"
    )


def test_app_page_shows_shareable_qr(server, page, channel):
    page.goto(server.base + "/a#codes=" + channel)
    # a QR for sharing this channel is always visible in the channel card
    page.wait_for_selector(".channel .qrshare svg")
    share_url = page.text_content(".channel .share-url")
    assert share_url.endswith("/a#codes=" + channel)
    # the QR graphic is labelled with the app name and the channel name
    assert "Join NotifyByWebApp" in page.text_content(".channel .share-app")
    page.wait_for_selector(".channel .share-channel:has-text('Test Channel')")


def test_app_page_message_order_and_local_timestamps(server, page, channel):
    server.post("/api/message", {"code": channel, "title": "First", "body": "1"})
    server.post("/api/message", {"code": channel, "title": "Second", "body": "2"})
    page.goto(server.base + "/a#codes=" + channel)
    page.wait_for_selector(".channel .msg-title")
    titles = page.eval_on_selector_all(
        ".channel .msg-title", "els => els.map(e => e.textContent)"
    )
    assert titles[0] == "Second" and titles[1] == "First"  # newest on top
    # every message shows a (non-empty) timestamp and a "newest first" hint
    times = page.eval_on_selector_all(
        ".channel .msg-time", "els => els.map(e => e.textContent.trim())"
    )
    assert len(times) >= 2 and all(times)
    assert page.query_selector(".channel .msgs-hint") is not None


def test_app_page_persists_codes_without_fragment(server, page, channel):
    page.goto(server.base + "/a#codes=" + channel)
    page.wait_for_selector(".channel h2:has-text('Test Channel')")

    # reopen without the fragment: localStorage keeps the channel
    page.goto(server.base + "/a")
    page.wait_for_selector(".channel h2:has-text('Test Channel')")

    # remove the channel; reopening WITH the fragment must not resurrect it
    page.click(".channel button.danger")
    page.wait_for_selector("#empty-hint:not([hidden])")
    page.goto(server.base + "/a#codes=" + channel)
    page.wait_for_selector("#empty-hint:not([hidden])")
    assert page.query_selector(".channel") is None

    # adding it again in-app clears the removed marker
    page.fill("#add-input", channel)
    page.click("#add-btn")
    page.wait_for_selector(".channel h2:has-text('Test Channel')")


def test_unknown_code_shows_friendly_error(server, page):
    page.goto(server.base + "/a#codes=this_code_does_not_exist_123456")
    page.wait_for_selector(".channel h2:has-text('Unknown channel')")

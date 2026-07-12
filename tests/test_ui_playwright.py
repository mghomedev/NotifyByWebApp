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

    # a second (typed) code extends the link (combining is under the expander)
    page.click(".combine summary")
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

    # returning to the page restores the channel (visible via the app link + QR)
    page.goto(server.base + "/")
    page.wait_for_selector("#link-result:not([hidden])")
    assert code in page.text_content("#app-url")
    assert page.is_checked("#save-consent")

    # "forget" clears the saved channels
    page.click("#forget-btn")
    page.goto(server.base + "/")
    assert page.is_hidden("#link-result")
    assert page.query_selector("#code-list .codes-item") is None


def test_landing_saved_channels_persist_to_both_stores_and_survive_cookie_loss(server, page):
    page.goto(server.base + "/")
    page.fill("#channel-name", "Durable")
    page.click("#create-btn")
    page.wait_for_selector("#create-result:not([hidden])")
    code = page.text_content("#new-code").strip()
    page.check("#save-consent")
    page.click("#save-btn")
    # written to BOTH a cookie and localStorage
    assert any(c["name"] == "nbw_codes" for c in page.context.cookies())
    assert code in (page.evaluate("localStorage.getItem('nbw_saved_codes')") or "")

    # survives losing the cookie (e.g. Safari's ~7-day cap on JS cookies)
    page.context.clear_cookies()
    page.reload()
    page.wait_for_selector("#link-result:not([hidden])")
    assert code in page.text_content("#app-url")
    # the heal step rewrote the dropped cookie
    assert any(c["name"] == "nbw_codes" for c in page.context.cookies())


def test_landing_saved_channels_survive_localstorage_loss(server, page):
    page.goto(server.base + "/")
    page.fill("#channel-name", "Durable2")
    page.click("#create-btn")
    page.wait_for_selector("#create-result:not([hidden])")
    code = page.text_content("#new-code").strip()
    page.check("#save-consent")
    page.click("#save-btn")
    # drop only localStorage; the cookie should restore the channels
    page.evaluate("localStorage.removeItem('nbw_saved_codes')")
    page.reload()
    page.wait_for_selector("#link-result:not([hidden])")
    assert code in page.text_content("#app-url")
    assert code in (page.evaluate("localStorage.getItem('nbw_saved_codes')") or "")


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


def test_app_page_send_password_required(server, page):
    code = server.post(
        "/api/channel", {"name": "Locked", "send_password": "manager-key"}
    ).json["code"]
    page.goto(server.base + "/a#codes=" + code)
    # the card advertises that sending needs a password and shows the field
    page.wait_for_selector(".channel details summary:has-text('password required')")
    page.click(".channel details summary")
    page.wait_for_selector(".channel .send-pw:not([hidden])")

    # sending without the password is rejected
    page.fill(".channel input[placeholder^='Title']", "Hello")
    page.click(".channel details button")
    page.wait_for_selector(".channel details:has-text('requires a valid send password')")

    # sending with the correct password works
    page.fill(".channel .send-pw", "manager-key")
    page.fill(".channel input[placeholder^='Title']", "Hello again")
    page.click(".channel details button")
    page.wait_for_selector(".channel .msg-title:has-text('Hello again')")


def test_app_page_sorts_channels_by_latest_event(server, page):
    import json

    import notify_core as core

    older = server.post("/api/channel", {"name": "Older"}).json["code"]
    newer = server.post("/api/channel", {"name": "Newer"}).json["code"]
    # give "Newer" a message with a clearly later timestamp than either creation
    core.get_storage().add_message(
        core.code_hash(newer),
        json.dumps({"id": "m", "ts": 2000000000, "title": "Latest", "body": "hi", "url": ""}),
        50,
    )
    page.goto(server.base + "/a#codes=" + older + "," + newer)
    # wait until both cards have been refreshed (data-ts set)
    page.wait_for_function(
        "document.querySelectorAll('.channel[data-ts]').length >= 2"
    )
    names = page.eval_on_selector_all(".channel h2", "els => els.map(e => e.textContent)")
    assert names[0] == "Newer"  # most recent activity on top
    # each card shows its latest-event time in small font
    latest = page.text_content(".channel:first-child .channel-latest")
    assert latest.startswith("Latest:")


def test_app_page_delete_message_and_clear_all(server, page, channel):
    expect = playwright_api.expect
    server.post("/api/message", {"code": channel, "title": "First"})
    server.post("/api/message", {"code": channel, "title": "Second"})
    page.goto(server.base + "/a#codes=" + channel)
    expect(page.locator(".channel .msg")).to_have_count(2)

    # delete the newest message via its trash icon (page fixture auto-accepts confirm)
    page.locator(".channel .msg-del").first.click()
    page.wait_for_selector(".channel .msg-title:has-text('Second')", state="detached")
    expect(page.locator(".channel .msg")).to_have_count(1)
    assert page.locator(".channel .msg-title").first.text_content() == "First"

    # clear all remaining messages via the header trash
    page.locator(".channel .msgs-hdr .iconbtn").click()
    page.wait_for_selector(".channel .msgs:has-text('No messages yet')")
    expect(page.locator(".channel .msg")).to_have_count(0)


def test_app_page_more_expander_and_delete_older(server, page, channel):
    expect = playwright_api.expect
    for i in range(5):
        server.post("/api/message", {"code": channel, "title": f"m{i}"})
    page.goto(server.base + "/a#codes=" + channel)
    # only the newest 3 are shown outside the expander
    expect(page.locator(".channel .msgs > .msg")).to_have_count(3)
    visible = page.eval_on_selector_all(
        ".channel .msgs > .msg .msg-title", "els => els.map(e => e.textContent)"
    )
    assert visible == ["m4", "m3", "m2"]

    # the older two live in a collapsed "More …" expander
    more = page.locator(".channel .more-msgs")
    expect(more).to_have_count(1)
    assert "More" in more.locator("summary").text_content()
    assert more.locator(".msg").count() == 2
    assert more.evaluate("el => el.open") is False  # collapsed by default
    expect(more.locator(".msg").first).to_be_hidden()  # older ones hidden

    # expand and delete the older ones (keeps newest 3); expander disappears
    more.locator("summary").click()
    more.locator(".msgs-hdr .iconbtn").click()
    page.wait_for_selector(".channel .more-msgs", state="detached")
    expect(page.locator(".channel .msg")).to_have_count(3)


def test_unknown_code_shows_friendly_error(server, page):
    page.goto(server.base + "/a#codes=this_code_does_not_exist_123456")
    page.wait_for_selector(".channel h2:has-text('Unknown channel')")

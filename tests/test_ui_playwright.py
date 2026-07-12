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
    # the created channel is listed VISIBLY (not hidden in an expander)
    assert page.is_visible("#your-channels")
    assert code in page.text_content("#code-list")
    assert page.locator("#code-list .codes-item").first.is_visible()

    # a second (typed) code extends the link (adding is under the expander)
    page.click(".combine summary")
    page.fill("#code-input", "a_second_code_0123456789")
    page.click("#add-code")
    app_url2 = page.text_content("#app-url").strip()
    assert app_url2.endswith(code + ",a_second_code_0123456789")

    # invalid codes are rejected with a message
    page.fill("#code-input", "nope")
    page.click("#add-code")
    assert "does not look like" in page.text_content("#add-error")


def test_landing_send_message_and_autosave(server, page):
    page.goto(server.base + "/")
    page.fill("#channel-name", "Send UI")
    page.click("#create-btn")
    page.wait_for_selector("#create-result:not([hidden])")
    code = page.text_content("#new-code").strip()

    # the send form and the developer curl example are prefilled with the code
    assert page.input_value("#send-code") == code
    assert page.text_content("#curl-code") == code
    # the channel is saved automatically (no opt-in) — confirmation shown
    assert page.is_visible("#create-saved")
    assert "saved" in page.text_content("#save-status").lower()

    # send a message straight from the landing page
    page.fill("#send-title", "Hello from landing")
    page.click("#send-btn")
    page.wait_for_selector("#send-ok:not([hidden])")
    assert "no device" in page.text_content("#send-ok").lower()  # sent=0, none yet
    snap = server.post("/api/messages", {"code": code}).json
    assert snap["messages"][0]["title"] == "Hello from landing"

    # returning to the page restores the channel automatically (no save click)
    # AND lists it visibly on the generator (regression: it must not be hidden in an
    # expander). We use /?create because a bare / now routes returning visitors with
    # saved channels straight to the app (covered by the redirect test below).
    page.goto(server.base + "/?create")
    page.wait_for_selector("#link-result:not([hidden])")
    assert code in page.text_content("#app-url")
    assert page.is_visible("#your-channels")
    assert page.locator("#code-list .codes-item").first.is_visible()
    assert code in page.text_content("#code-list")

    # "Forget & stop saving" clears them and turns saving off — and, because the opt-out
    # flag also disables the returning-visitor redirect, a bare / stays on the generator
    page.click("#forget-btn")
    page.goto(server.base + "/")
    assert "/a" not in page.url
    assert page.is_hidden("#link-result")
    assert "off" in page.text_content("#save-status").lower()
    assert page.is_visible("#save-btn")  # re-enable button offered

    # re-enabling saving works again
    page.click("#save-btn")
    page.fill("#channel-name", "Again")
    page.click("#create-btn")
    page.wait_for_selector("#create-result:not([hidden])")
    code2 = page.text_content("#new-code").strip()
    page.goto(server.base + "/?create")
    page.wait_for_selector("#link-result:not([hidden])")
    assert code2 in page.text_content("#app-url")


def test_landing_autosaved_channels_survive_cookie_loss(server, page):
    page.goto(server.base + "/")
    page.fill("#channel-name", "Durable")
    page.click("#create-btn")
    page.wait_for_selector("#create-result:not([hidden])")
    code = page.text_content("#new-code").strip()
    # auto-saved to BOTH a cookie and localStorage (no opt-in)
    assert any(c["name"] == "nbw_codes" for c in page.context.cookies())
    assert code in (page.evaluate("localStorage.getItem('nbw_saved_codes')") or "")

    # survives losing the cookie (e.g. Safari's ~7-day cap on JS cookies). Reopen the
    # generator via /?create (a bare / would route us to the app now that a channel is
    # saved); localStorage restores the channel and re-heals the cookie.
    page.context.clear_cookies()
    page.goto(server.base + "/?create")
    page.wait_for_selector("#link-result:not([hidden])")
    assert code in page.text_content("#app-url")
    assert any(c["name"] == "nbw_codes" for c in page.context.cookies())  # cookie healed


def test_landing_autosaved_channels_survive_localstorage_loss(server, page):
    page.goto(server.base + "/")
    page.fill("#channel-name", "Durable2")
    page.click("#create-btn")
    page.wait_for_selector("#create-result:not([hidden])")
    code = page.text_content("#new-code").strip()
    # drop only localStorage; the cookie restores it, and the heal rewrites localStorage.
    # Reopen via /?create so we stay on the generator (a bare / would route to the app).
    page.evaluate("localStorage.removeItem('nbw_saved_codes')")
    page.goto(server.base + "/?create")
    page.wait_for_selector("#link-result:not([hidden])")
    assert code in page.text_content("#app-url")
    assert code in (page.evaluate("localStorage.getItem('nbw_saved_codes')") or "")


def test_landing_routes_returning_visitor_to_saved_channels(server, page):
    # a fresh visitor (no saved channels) sees the generator, not a redirect
    page.goto(server.base + "/")
    page.wait_for_selector("#create-btn")
    assert "/a" not in page.url

    # create a channel — it is auto-saved to the cookie + localStorage
    page.fill("#channel-name", "Homecoming")
    page.click("#create-btn")
    page.wait_for_selector("#create-result:not([hidden])")
    code = page.text_content("#new-code").strip()

    # a returning visit to a bare / now routes straight into the app, so the page STARTS
    # with the user's saved channels + their messages + send, not the create form
    page.goto(server.base + "/", wait_until="commit")
    page.wait_for_selector(".channel")
    assert "/a#codes=" in page.url
    assert code in page.url

    # /?create is the escape hatch: it always shows the generator (to make a new channel),
    # and still lists the saved channel visibly there
    page.goto(server.base + "/?create")
    page.wait_for_selector("#create-btn")
    assert "/a" not in page.url
    assert page.is_visible("#your-channels")
    assert code in page.text_content("#code-list")
    # the prominent "Open my channels & messages" button is shown and points at the app URL
    assert page.is_visible("#have-channels")
    assert page.get_attribute("#open-app-top", "href").endswith("/a#codes=" + code)


def test_landing_nosave_disables_redirect_even_with_saved_channels(server, page):
    # opting out of saving must suppress the returning-visitor redirect even while channels are
    # still present in the store (isolates the nbw_nosave guard from the empty-store guard)
    code = server.post("/api/channel", {"name": "OptOut"}).json["code"]
    page.goto(server.base + "/?create")
    page.evaluate(
        "(c)=>{localStorage.setItem('nbw_saved_codes',JSON.stringify([c]));"
        "document.cookie='nbw_codes='+c+';path=/';"
        "localStorage.setItem('nbw_nosave','1');}",
        code,
    )
    page.goto(server.base + "/")
    page.wait_for_selector("#create-btn")
    assert "/a" not in page.url  # generator shown, not routed to the app


def test_app_page_unions_shared_store_and_legacy_list(server, page):
    # A channel that lives only in the app's legacy list (e.g. pasted in-app before the stores
    # were unified) must NOT be dropped when a different channel lives in the shared store.
    a = server.post("/api/channel", {"name": "SharedA"}).json["code"]
    b = server.post("/api/channel", {"name": "LegacyB"}).json["code"]
    page.goto(server.base + "/a")  # establish the origin so localStorage/cookies are writable
    page.evaluate(
        "([a,b])=>{localStorage.setItem('nbw_saved_codes',JSON.stringify([a]));"
        "document.cookie='nbw_codes='+a+';path=/';"
        "localStorage.setItem('nbw_codes',JSON.stringify([b]));}",
        [a, b],
    )
    page.goto(server.base + "/a")  # no fragment: loadCodes must UNION shared store + legacy list
    page.wait_for_selector(".channel h2:has-text('SharedA')")
    page.wait_for_selector(".channel h2:has-text('LegacyB')")


def test_app_page_loads_from_shared_store_without_legacy_or_fragment(server, page):
    # A channel present ONLY in the cross-page shared store (cookie nbw_codes + LS nbw_saved_codes)
    # must render on /a with no fragment and no legacy LS_CODES — isolates readSavedStore().
    code = server.post("/api/channel", {"name": "SharedOnly"}).json["code"]
    page.goto(server.base + "/a")
    page.evaluate(
        "(c)=>{localStorage.setItem('nbw_saved_codes',JSON.stringify([c]));"
        "document.cookie='nbw_codes='+c+';path=/';"
        "localStorage.removeItem('nbw_codes');}",
        code,
    )
    page.goto(server.base + "/a")
    page.wait_for_selector(".channel h2:has-text('SharedOnly')")


def test_landing_removal_tombstones_so_app_does_not_resurrect(server, page):
    # Removing a channel on the landing page must record a shared tombstone so a stale install-URL
    # fragment (or the app's legacy list) cannot resurrect it on /a.
    code = server.post("/api/channel", {"name": "ToRemove"}).json["code"]
    page.goto(server.base + "/a#codes=" + code)  # app now has it in LS_CODES + shared store
    page.wait_for_selector(".channel h2:has-text('ToRemove')")
    page.goto(server.base + "/?create")  # go to the generator and remove it there
    page.wait_for_selector("#code-list .codes-item")
    page.click("#code-list .codes-item button.danger")
    page.wait_for_selector("#code-list .codes-item", state="detached")
    # reopen the app via the STALE fragment: the removed channel must NOT come back
    page.goto(server.base + "/a#codes=" + code)
    page.wait_for_selector("#empty-hint:not([hidden])")
    assert page.query_selector(".channel") is None


def test_app_page_escape_hatch_link_reaches_generator(server, page, channel):
    # From the installed app, the "start page" link must reach the generator (/?create), NOT get
    # bounced straight back to /a by the returning-visitor redirect.
    page.goto(server.base + "/a#codes=" + channel)
    page.wait_for_selector(".channel")
    page.click("footer a:has-text('start page')")
    page.wait_for_selector("#create-btn")  # would time out if it bounced back to /a
    assert "create" in page.url


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


def test_app_page_auto_refresh_toast_and_highlight(server, browser, channel):
    ctx = browser.new_context()
    ctx.add_init_script("window.__NBW_POLL_MS = 500;")  # poll fast for the test
    pg = ctx.new_page()
    pg.on("dialog", lambda d: d.accept())
    try:
        pg.goto(server.base + "/a#codes=" + channel)
        pg.wait_for_selector(".channel .msgs")
        assert pg.locator("#toasts .toast").count() == 0  # no toast for baseline

        # a message arrives from elsewhere; the page is NOT reloaded
        server.post("/api/message", {"code": channel, "title": "Live ping", "body": "hi"})
        # auto-refresh brings it in, highlighted (one per channel) with a NEW badge
        pg.wait_for_selector(".channel .msg.msg-new .msg-title:has-text('Live ping')", timeout=8000)
        assert pg.locator(".channel .msg-new").count() == 1
        assert pg.locator(".msg-new-badge").count() == 1

        # an in-app toast appears with the content and the three actions
        pg.wait_for_selector("#toasts .toast:has-text('Live ping')", timeout=8000)
        assert "New message" in pg.text_content("#toasts .toast")
        for label in ("Go to channel", "Reply", "Delete"):
            assert pg.locator(".toast-btn", has_text=label).count() >= 1

        # Reply opens the channel's send dialog
        pg.locator("#toasts .toast").first.locator(".toast-btn", has_text="Reply").click()
        pg.wait_for_selector(".channel .send-details[open]")

        # a second arrival moves the single highlight to the newest
        server.post("/api/message", {"code": channel, "title": "Second ping"})
        pg.wait_for_selector(".channel .msg.msg-new .msg-title:has-text('Second ping')", timeout=8000)
        assert pg.locator(".channel .msg-new").count() == 1

        # Delete on the newest toast removes that message
        pg.locator("#toasts .toast", has_text="Second ping").locator(".toast-del").click()
        pg.wait_for_selector(".channel .msg-title:has-text('Second ping')", state="detached", timeout=8000)
    finally:
        ctx.close()


def test_app_page_body_only_message_not_shown_twice(server, page, channel):
    # A body-only short message: the server derives title==body, so the in-app list must show
    # the text ONCE (a title, no duplicate body line) — never "Hi / Hi".
    server.post("/api/message", {"code": channel, "body": "Hi"})
    page.goto(server.base + "/a#codes=" + channel)
    page.wait_for_selector(".channel .msg-title:has-text('Hi')")
    assert page.locator(".channel .msg-body").count() == 0
    assert page.locator(".channel .msg-title", has_text="Hi").count() == 1
    # a genuinely distinct body is still shown in full
    server.post("/api/message", {"code": channel, "title": "T", "body": "a different body"})
    page.wait_for_selector(".channel .msg-body:has-text('a different body')")
    assert page.locator(".channel .msg-body").count() == 1


def test_app_page_toast_does_not_duplicate_title_as_body(server, browser, channel):
    # the in-app new-message toast must not repeat the title as its body either
    ctx = browser.new_context()
    ctx.add_init_script("window.__NBW_POLL_MS = 500;")
    pg = ctx.new_page()
    pg.on("dialog", lambda d: d.accept())
    try:
        pg.goto(server.base + "/a#codes=" + channel)
        pg.wait_for_selector(".channel .msgs")
        server.post("/api/message", {"code": channel, "body": "Hi"})  # derives title==body
        pg.wait_for_selector("#toasts .toast:has-text('Hi')", timeout=8000)
        txt = pg.text_content("#toasts .toast")
        assert "Hi / Hi" not in txt  # the content is not duplicated
    finally:
        ctx.close()


def test_unknown_code_shows_friendly_error(server, page):
    page.goto(server.base + "/a#codes=this_code_does_not_exist_123456")
    page.wait_for_selector(".channel h2:has-text('Unknown channel')")

"""Browser notification tests — the real Chrome/Android push pipeline.

A real push is delivered into the registered service worker via CDP
(ServiceWorker.deliverPushMessage, the same entry point Chrome uses for an
FCM push) and we assert the notification the SW displays has the right
title/body/tag/click-data. This is the automated proof that "a notification
actually shows up on the device".

These run a HEADED browser on purpose: headless Chromium refuses notification
permission (showNotification throws "No notification permission"), so a real
notification can only be displayed and read back with a real browser window.
Auto-skips without Chromium or without a display:
    python -m playwright install chromium
"""
import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from uikit import deliver_push, wait_for_active_sw, wait_for_notification


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            # headed: headless denies the notification permission we need
            b = p.chromium.launch(headless=False)
        except Exception:
            pytest.skip("chromium/display unavailable for headed notification tests")
        yield b
        b.close()


@pytest.fixture()
def notif_page(browser, server, channel):
    ctx = browser.new_context()
    ctx.grant_permissions(["notifications"], origin=server.base)
    page = ctx.new_page()
    page.goto(server.base + "/a#codes=" + channel)
    wait_for_active_sw(page)
    yield page, server.base, channel
    ctx.close()


def test_push_displays_notification_with_correct_content(notif_page):
    page, base, channel = notif_page
    deliver_push(
        page,
        base,
        {
            "title": "Deploy done",
            "body": "main is green ✓",
            "url": "https://ci.example.com/run/9",
            "channel": "Test Channel",
            "tag": "msg-abc",
            "ts": 1_700_000_000,
        },
    )
    notes = wait_for_notification(page)
    assert len(notes) == 1
    n = notes[0]
    # SW prefixes the channel name onto the title
    assert n["title"] == "Deploy done — Test Channel"
    assert n["body"] == "main is green ✓"
    assert n["tag"] == "msg-abc"
    assert n["data"]["url"] == "https://ci.example.com/run/9"
    assert n["icon"].endswith("/icon-192.png")


def test_push_without_url_targets_app_page(notif_page):
    page, base, channel = notif_page
    deliver_push(page, base, {"title": "No link", "body": "hi"})
    notes = wait_for_notification(page)
    assert len(notes) == 1
    assert notes[0]["title"] == "No link"
    # click target defaults to the app page
    assert notes[0]["data"]["url"] == "/a"


def test_push_body_equal_to_title_not_duplicated(notif_page):
    # A body-only message derives title==body on the server; the browser notification must
    # show the text once (empty body), not "Hi" as both the heading and the body.
    page, base, channel = notif_page
    deliver_push(page, base, {"title": "Hi", "body": "Hi", "channel": "Test Channel"})
    notes = wait_for_notification(page)
    assert len(notes) == 1
    assert notes[0]["title"] == "Hi — Test Channel"
    assert notes[0]["body"] == ""


def test_multiple_pushes_show_as_distinct_notifications(notif_page):
    page, base, channel = notif_page
    deliver_push(page, base, {"title": "First", "body": "1", "tag": "t1"})
    deliver_push(page, base, {"title": "Second", "body": "2", "tag": "t2"})
    deadline_notes = []
    # wait until both are present
    for _ in range(50):
        deadline_notes = wait_for_notification(page)
        if len(deadline_notes) >= 2:
            break
        page.wait_for_timeout(100)
    titles = sorted(n["title"] for n in deadline_notes)
    assert titles == ["First", "Second"]


def test_malformed_push_still_shows_a_notification(notif_page):
    # Chrome requires a visible notification for every push (userVisibleOnly);
    # our SW must cope with a non-JSON payload and still show something.
    page, base, channel = notif_page
    session = page.context.new_cdp_session(page)
    from uikit import _registration_id

    reg_id = _registration_id(page, session, base)
    session.send(
        "ServiceWorker.deliverPushMessage",
        {"origin": base, "registrationId": reg_id, "data": "not-json-at-all"},
    )
    session.detach()
    notes = wait_for_notification(page)
    assert len(notes) == 1
    assert notes[0]["title"] == "Notify"  # fallback title


def test_push_under_android_device_emulation(browser, server, channel):
    """Same pipeline under a Pixel mobile emulation profile (touch, mobile
    viewport, Android UA) — the Android PWA runtime is Chrome."""
    pixel = dict(
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
        ),
        viewport={"width": 412, "height": 915},
        device_scale_factor=2.625,
        is_mobile=True,
        has_touch=True,
    )
    ctx = browser.new_context(**pixel)
    ctx.grant_permissions(["notifications"], origin=server.base)
    try:
        page = ctx.new_page()
        page.goto(server.base + "/a#codes=" + channel)
        wait_for_active_sw(page)
        deliver_push(page, server.base, {"title": "Android", "body": "works"})
        notes = wait_for_notification(page)
        assert len(notes) == 1
        assert notes[0]["title"] == "Android"
    finally:
        ctx.close()

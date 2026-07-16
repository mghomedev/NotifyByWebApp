"""Platform-behavior emulation for the notification UX.

Chromium is one engine, but the app must behave correctly across the two
platform situations that matter for Web Push:

1. iOS Safari TAB (not installed): the Push/Notification APIs are absent, so
   the app must show "install to Home Screen first" and hide the enable
   button — never silently fail.
2. iOS INSTALLED (standalone) / Android: the push APIs exist and the
   enable->subscribe->/api/subscribe flow must actually register the device
   on the server and report ON only on success.

We emulate (1) by removing the Push/Notification APIs and presenting an
iPhone UA + non-standalone display (what Safari-in-a-tab looks like), and
(2) by injecting a working fake push stack so the subscribe wiring and UI
state are exercised deterministically.

Auto-skips without Chromium.
"""
import pytest

playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("chromium not installed (python -m playwright install chromium)")
        yield b
        b.close()


IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Mobile/15E148 Safari/604.1"
)

# Remove the Web Push APIs to emulate an iOS Safari browser tab (no PWA push).
STRIP_PUSH_APIS = """
try { delete window.PushManager; } catch (e) {}
try { delete window.Notification; } catch (e) {}
Object.defineProperty(navigator, 'standalone', {configurable:true, get:()=>false});
"""


IOS15_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
)
ANDROID6_UA = (
    "Mozilla/5.0 (Linux; Android 6.0; SM-G900) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/50.0 Mobile Safari/537.36"
)


def test_old_iphone_shows_too_old_warning(browser, server, channel):
    ctx = browser.new_context(user_agent=IOS15_UA, has_touch=True)
    ctx.add_init_script(STRIP_PUSH_APIS)
    try:
        page = ctx.new_page()
        page.goto(server.base + "/a#codes=" + channel)
        page.wait_for_selector("#too-old:not([hidden])")
        txt = page.text_content("#too-old")
        assert "too old" in txt.lower() and "16.4" in txt and "iPhone" in txt
    finally:
        ctx.close()


def test_old_android_shows_too_old_warning(browser, server, channel):
    ctx = browser.new_context(user_agent=ANDROID6_UA, has_touch=True)
    ctx.add_init_script(STRIP_PUSH_APIS)
    try:
        page = ctx.new_page()
        page.goto(server.base + "/a#codes=" + channel)
        page.wait_for_selector("#too-old:not([hidden])")
        assert "Android" in page.text_content("#too-old")
    finally:
        ctx.close()


def test_modern_installed_device_has_no_too_old_warning(browser, server, channel):
    ctx = browser.new_context(user_agent=IPHONE_UA, has_touch=True)
    ctx.add_init_script(FAKE_STANDALONE_PUSH)  # push primitives present => supported
    try:
        page = ctx.new_page()
        page.goto(server.base + "/a#codes=" + channel)
        page.wait_for_selector(".channel")
        assert page.is_hidden("#too-old")
    finally:
        ctx.close()


def test_ios_safari_tab_shows_install_instructions(browser, server, channel):
    ctx = browser.new_context(
        user_agent=IPHONE_UA,
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
    )
    ctx.add_init_script(STRIP_PUSH_APIS)
    try:
        page = ctx.new_page()
        page.goto(server.base + "/a#codes=" + channel)
        # the iOS install hint banner is shown
        page.wait_for_selector("#ios-hint:not([hidden])")
        # the enable button is hidden and the state explains why
        assert page.is_hidden("#enable-btn")
        state = page.text_content("#notif-state").lower()
        assert "install" in state or "home screen" in state
        # sending still works even without push (the channel card renders)
        page.wait_for_selector(".channel h2:has-text('Test Channel')")
    finally:
        ctx.close()


# A minimal but functional fake push stack for the "installed PWA" case: a
# service worker registration whose pushManager hands out a valid-looking
# subscription (real push host so it passes the server's SSRF/format checks).
FAKE_STANDALONE_PUSH = """
Object.defineProperty(navigator, 'standalone', {configurable:true, get:()=>true});
window.Notification = function(){};
window.Notification.permission = 'granted';
window.Notification.requestPermission = () => Promise.resolve('granted');
(function(){
  var sub = {
    endpoint: 'https://fcm.googleapis.com/fcm/send/emu-' + Math.random().toString(36).slice(2),
    options: { applicationServerKey: null },
    _active: false,
    toJSON: function(){ return { endpoint: this.endpoint,
      keys: { p256dh: 'BEmulatedPublicKeyAAAAAAAAAAAAAAAAAAAA', auth: 'emulatedAuthAAAAAAAA' } }; },
    unsubscribe: function(){ this._active = false; return Promise.resolve(true); }
  };
  var reg = {
    scope: location.origin + '/',
    pushManager: {
      getSubscription: function(){ return Promise.resolve(sub._active ? sub : null); },
      subscribe: function(opts){ sub.options.applicationServerKey =
        opts && opts.applicationServerKey; sub._active = true; return Promise.resolve(sub); }
    },
    showNotification: function(){ return Promise.resolve(); },
    getNotifications: function(){ return Promise.resolve([]); }
  };
  var fakeSW = {
    register: function(){ return Promise.resolve(reg); },
    ready: Promise.resolve(reg),
    getRegistrations: function(){ return Promise.resolve([reg]); },
    getRegistration: function(){ return Promise.resolve(reg); },
    addEventListener: function(){},
    controller: null
  };
  Object.defineProperty(navigator, 'serviceWorker', {configurable:true, get:()=>fakeSW});
})();
"""


# True iff the Enable-notifications card sits ABOVE the channels list in the DOM.
NOTIF_FIRST_JS = (
    "() => {var n=document.getElementById('notif-card'),"
    "c=document.getElementById('channels');"
    "return !!(n.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING);}"
)


def test_ios_installed_pwa_subscribe_registers_on_server(browser, server, channel):
    ctx = browser.new_context(
        user_agent=IPHONE_UA,
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
    )
    ctx.add_init_script(FAKE_STANDALONE_PUSH)
    try:
        page = ctx.new_page()
        page.goto(server.base + "/a#codes=" + channel)
        page.wait_for_selector(".channel h2:has-text('Test Channel')")

        # no install banner in standalone mode
        assert page.is_hidden("#ios-hint")

        # NOT enabled yet: the Enable prompt must be ABOVE the channels, and its warning
        # that this turns on system notifications (working even when closed) is shown
        assert page.evaluate(NOTIF_FIRST_JS) is True
        assert page.is_visible("#notif-why")
        assert "closed" in page.text_content("#notif-why").lower()

        # user taps "Enable notifications"
        page.click("#enable-btn")

        # UI reports ON only after the server accepted the subscription
        page.wait_for_selector("#notif-state.status-ok")
        assert "on" in page.text_content("#notif-state").lower()

        # once enabled, the channels lead (prompt drops below) and the warning is gone
        page.wait_for_function(
            "() => {var n=document.getElementById('notif-card'),"
            "c=document.getElementById('channels');"
            "return !!(c.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING);}"
        )
        assert page.is_hidden("#notif-why")

        # and the server really recorded this device as a subscriber
        page.wait_for_function(
            """async () => {
                const r = await fetch('/api/messages', {method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({code: %r})});
                const j = await r.json();
                return j.subscribers >= 1;
            }"""
            % channel
        )
    finally:
        ctx.close()


def _subscribers_js(code):
    return (
        "async () => { const r = await fetch('/api/messages', {method:'POST',"
        "headers:{'Content-Type':'application/json'},body: JSON.stringify({code: %r})});"
        "return (await r.json()).subscribers; }" % code
    )


def test_mute_unsubscribes_and_unmute_resubscribes(browser, server):
    code = server.post("/api/channel", {"name": "Muteable"}).json["code"]
    ctx = browser.new_context(user_agent=IPHONE_UA, has_touch=True)
    ctx.add_init_script(FAKE_STANDALONE_PUSH)
    try:
        page = ctx.new_page()
        page.goto(server.base + "/a#codes=" + code)
        page.wait_for_selector(".channel")
        page.click("#enable-btn")
        # subscribed on this device
        page.wait_for_function("(%s)().then(n => n >= 1)" % _subscribers_js(code))

        # mute -> the channel's endpoint is unsubscribed on the server
        page.click(".channel .mute-btn")
        page.wait_for_function("(%s)().then(n => n === 0)" % _subscribers_js(code))
        assert "Unmute" in page.text_content(".channel .mute-btn")

        # unmute -> re-subscribed
        page.click(".channel .mute-btn")
        page.wait_for_function("(%s)().then(n => n >= 1)" % _subscribers_js(code))
        assert "Unmute" not in page.text_content(".channel .mute-btn")
    finally:
        ctx.close()


def test_installed_pwa_reports_error_when_server_rejects(browser, server):
    """If every /api/subscribe fails (here: an unknown/expired channel -> 404),
    the app must NOT claim notifications are ON — it must surface an error and
    keep the enable button available (the false-'ON' regression guard)."""
    import notify_core as core

    ghost = core.generate_code()  # never created -> subscribe returns 404
    ctx = browser.new_context(
        user_agent=IPHONE_UA, viewport={"width": 390, "height": 844}, has_touch=True
    )
    ctx.add_init_script(FAKE_STANDALONE_PUSH)
    try:
        page = ctx.new_page()
        page.goto(server.base + "/a#codes=" + ghost)
        page.wait_for_selector(".channel")
        page.click("#enable-btn")
        # must land in the failure state, not the green ON state
        page.wait_for_selector("#notif-state.err")
        assert page.is_visible("#enable-btn")
        state = page.text_content("#notif-state").lower()
        assert "could not" in state or "try again" in state
        # nbw_subscribed must not be a truthy 'confirmed' ON — the app may keep
        # opt-in intent, but the UI is not allowed to show ON
        assert "status-ok" not in (page.get_attribute("#notif-state", "class") or "")
    finally:
        ctx.close()

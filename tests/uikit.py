"""Helpers for the browser (Playwright) tests: deliver a real push into the
service worker via the Chrome DevTools Protocol and read back the displayed
notifications.

ServiceWorker.deliverPushMessage is exactly what Chrome (and therefore
Android's Chrome-based PWA runtime) does when a push arrives from FCM — it
fires the 'push' event on the registered service worker with the given
payload. So asserting on the resulting notification proves the real
push -> service worker -> showNotification pipeline, headlessly.
"""
import json
import time


def wait_for_active_sw(page) -> None:
    page.evaluate("async () => { await navigator.serviceWorker.ready; return true; }")


def _registration_id(page, session, base: str, tries: int = 100) -> str:
    regs = {}

    def on_update(params):
        for r in params.get("registrations", []):
            regs[r["registrationId"]] = r.get("scopeURL", "")

    session.on("ServiceWorker.workerRegistrationUpdated", on_update)
    session.send("ServiceWorker.enable")
    # page.wait_for_timeout pumps Playwright's event loop so queued CDP events
    # are actually dispatched (time.sleep would not).
    for _ in range(tries):
        for rid, scope in list(regs.items()):
            if scope.startswith(base):
                return rid
        page.wait_for_timeout(100)
    raise AssertionError("no service-worker registration found via CDP")


def deliver_push(page, base: str, payload: dict) -> None:
    """Fire a push event on the page's service worker with `payload` (JSON)."""
    session = page.context.new_cdp_session(page)
    try:
        reg_id = _registration_id(page, session, base)
        session.send(
            "ServiceWorker.deliverPushMessage",
            {"origin": base, "registrationId": reg_id, "data": json.dumps(payload)},
        )
    finally:
        session.detach()


def get_notifications(page):
    return page.evaluate(
        """async () => {
            const r = await navigator.serviceWorker.ready;
            const list = await r.getNotifications();
            return list.map(n => ({title:n.title, body:n.body, tag:n.tag,
                                   data:n.data, icon:n.icon}));
        }"""
    )


def wait_for_notification(page, timeout_ms: int = 5000):
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        notes = get_notifications(page)
        if notes:
            return notes
        page.wait_for_timeout(100)
    return get_notifications(page)

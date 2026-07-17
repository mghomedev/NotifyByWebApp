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


def ch_of(code: str) -> str:
    """The channel id prefix used by push payloads and the device-local
    message store: sha256(code) first 12 hex chars (mirrors the client)."""
    import hashlib

    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]


_IDB_ALL_JS = """() => new Promise(res => {
    const rq = indexedDB.open('nbw', 1);
    rq.onupgradeneeded = () => rq.result.createObjectStore('msgs', {keyPath: 'k'});
    rq.onsuccess = () => {
        const db = rq.result;
        const q = db.transaction('msgs', 'readonly').objectStore('msgs').getAll();
        q.onsuccess = () => { db.close(); res(q.result || []); };
        q.onerror = () => { db.close(); res([]); };
    };
    rq.onerror = () => res([]);
})"""


def idb_all(page):
    """All records in the page's device-local message store (IndexedDB)."""
    return page.evaluate(_IDB_ALL_JS)


def seed_local(page, ch: str, msgs: list) -> None:
    """Insert message records directly into the device-local store.
    Each msg needs id/ts/title (body/url/name optional)."""
    records = [
        {
            "k": f"{ch}:{m['id']}",
            "ch": ch,
            "id": m["id"],
            "ts": m["ts"],
            "title": m.get("title", ""),
            "body": m.get("body", ""),
            "url": m.get("url", ""),
            "name": m.get("name", ""),
        }
        for m in msgs
    ]
    page.evaluate(
        """(records) => new Promise(res => {
        const rq = indexedDB.open('nbw', 1);
        rq.onupgradeneeded = () => rq.result.createObjectStore('msgs', {keyPath: 'k'});
        rq.onsuccess = () => {
            const db = rq.result;
            const tx = db.transaction('msgs', 'readwrite');
            const st = tx.objectStore('msgs');
            records.forEach(r => { try { st.put(r); } catch (e) {} });
            tx.oncomplete = () => { db.close(); res(true); };
            tx.onerror = () => { db.close(); res(false); };
        };
        rq.onerror = () => res(false);
    })""",
        records,
    )


def get_notifications(page):
    return page.evaluate(
        """async () => {
            const r = await navigator.serviceWorker.ready;
            const list = await r.getNotifications();
            return list.map(n => ({title:n.title, body:n.body, tag:n.tag,
                                   data:n.data, icon:n.icon, badge:n.badge}));
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

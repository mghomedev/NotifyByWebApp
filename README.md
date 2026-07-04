# NotifyByWebApp

A very lightweight **Progressive Web App (PWA)** for receiving push notifications on modern
Android and iOS smartphones — no app store, no user accounts.

> **Status: early development.** This README describes the goal and planned design.
> Nothing is deployed yet.

## The idea

1. **Anyone can create a message channel.** Creating a channel produces a long, secure,
   non-guessable channel key (also shown as a QR code).
2. **Subscribers** open a user-friendly page on their phone where they select or enter one
   or more channel codes (or scan their QR codes). The page then **generates a URL that
   contains all chosen codes — this URL is the app**: opened on the phone and added to the
   Home Screen, the installed PWA is pre-configured for exactly those channels. From then
   on subscribers receive channel messages as native push notifications via the platform's
   built-in PWA push support (Android, and iOS 16.4+ where the app must be installed to
   the Home Screen).
3. **Senders** — anyone who has the channel key — can send messages to all subscribers of
   that channel, either through a small web interface or through a plain HTTP API that any
   third-party service can call.

The key is the capability: possession of a channel key is all that's needed to subscribe
to the channel and to send messages to it. There are no accounts, no logins, and no
personal data beyond the technical push subscription.

## Planned features

- **PWA**: installable on the Home Screen of current Android and iOS phones, with real
  push notifications (Web Push / VAPID).
- **Channels**: created by anyone, identified by long random keys, shareable as QR codes.
- **Web interface**: per channel, a minimal page to send messages and see recent messages.
- **HTTP API**: send (and read) messages programmatically from any third-party service
  using the channel key.

## Technology

- Hosted on [Vercel](https://vercel.com) (Hobby tier) as a single lightweight app:
  Python serverless functions for the API plus a minimal static/inline web UI.
- Web Push with VAPID for notifications.
- Public codebase on GitHub. **No personal data and no secrets** (VAPID private key,
  database credentials, …) are ever committed to this repository; runtime secrets live
  only in local `.env` files (gitignored) and Vercel environment variables.

## Disclaimer

This is a hobby project without any warranty. It may stop working at any time and there
is no guarantee of support. Use at your own risk.

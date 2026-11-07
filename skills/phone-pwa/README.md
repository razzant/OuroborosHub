# Phone access for Ouroboros

Enable this extension, then open Widgets → Phone access. The host-owned companion prepares the pinned tunnel runtime, checks the selected live interface, starts its private gateway and tunnel, and reports actual core/public-address reachability. The widget creates a real QR, shows its ten-minute countdown, installation help and connected-device revocation. Refresh is read-only; Create QR and Retry tunnel are deliberate actions. Retry changes the address, so an existing phone icon must be reinstalled. A widget remount retrieves the active invitation without creating another one.

## Requirements and state

Ouroboros PluginAPI 2.0 with companion and module-widget support; network access to Cloudflare/GitHub. aiohttp 3.13.2 and qrcode 8.2 install through ordinary isolated dependencies. The bundled reusable MIT downloader verifies exact Cloudflared 2026.7.2 size/digests and version before execution. Supported assets are Windows x64, Linux x64/ARM64 and macOS Intel/Apple Silicon. No author-specific paths or credentials are shipped. Unsupported architecture or incompatible selected chat module reports an explicit startup failure; enable again after correcting prerequisites. No paid model calls, account or domain purchase.

Only the host-provided skill state root is used. runtime.json holds the current loopback core address; gateway/devices.sqlite holds hashes/labels/times and is retained across disable/re-enable. gateway/launch.json holds a private local-management bearer and must not be published. companion-status.json is a dated operational observation, not proof an old process is alive. The sole companion lock and exclusive listener ownership prevent concurrent gateways. Old pending QR links are per generation and do not survive restart.

## Access boundary

The public gateway and independent management listener both bind 127.0.0.1. ONLY the gateway is tunneled. Exact Host/Origin is checked, remote credentials/forwarding headers and upstream cookies are not forwarded. The widget talks through this skill's host routes; it never receives the management bearer. The phone proxy refuses this skill's direct management prefix. A paired phone nonetheless has FULL owner UI powers, including ordinary Files and runtime controls; this is NOT a sandbox or a secrecy boundary against a malicious paired owner. All such device sessions should be trusted as the owner. QR/link is private bearer material. Cloudflare terminates HTTPS and can transport plaintext: not end-to-end encryption against the provider.

A QR admits repeated independent sessions for 600 seconds, until its explicit revoke or gateway restart. Expiry/revoke QR stops only NEW access. Individual device revoke stops its current/pending gateway connections and prevents new ones. It cannot undo already accepted core actions. Device cookies expire after 30 days; current device records remain, but browser cookies are origin-scoped. New Quick Tunnel hostname means a new connection and home-screen icon.

Disable/unload/Panic ends the companion and tunnel under the host lifecycle. There is no OS-autostart installation. The Windows tunnel uses a kill-on-close Job; POSIX has an isolated-group pipe watchdog. A failed tunnel is not silently restarted to a new hostname: use Retry. The widget's timers/requests/theme subscription are disposed when it closes, not the long-lived companion. Cleanup of unique per-run tunnel homes is bounded by the reused helper; verified executable cache and devices persist.

## Phone installation

1. Scan the widget QR. Open in Safari/Chrome rather than an embedded browser; choose Connect this device and retain the link during installation.
2. iPhone/iOS 26: compact Safari More (…) → Share, or Top/Bottom Share → Add to Home Screen (Edit Actions if missing) → Open as Web App → Add. Launch that icon while the invitation is active; its separate storage may redeem the same link again. iOS 18 commonly omits Open as Web App.
3. Android/Chrome: ⋮ → Install and create shortcut → Install. Some versions use Add to home screen or Install app. A plain shortcut is not proof of standalone mode.
4. Keep the PC, Ouroboros and tunnel on. Reconnect through the PC's fresh QR after a hostname change. Existing authorized app/browser cookies are checked before a stale admission link.

References: https://support.apple.com/guide/iphone/iph42ab2f3a7/26.0/ios/26.0 ; https://support.google.com/chrome/answer/9658361?hl=en&co=GENIE.Platform%3DAndroid ; https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/ . Directions verified from documentation 3 November 2026; browser emulation does not certify physical Add to Home Screen or native keyboards.

## Interface and streams

No core files, pinned interface, Telegram, Hélène or Praxis are edited. The selected UI is read live. Existing sender/multiline/delta preview, draft/outbox and deliberate reading behavior stay with that UI. keyboard.py is an explicit response-only compatibility backport of PR2727's warm landing and deliberate older-scroll keyboard dismissal. It accepts the corrected form and rejects unknown bindings during startup instead of delivering a broken chat module. SOURCES.md names the owning contribution; this mitigation is not a repaired core.

There is no service worker or gateway private-data cache. Normal HTTP/binary and WebSocket traffic is proxied to one fixed loopback upstream. Quick Tunnel has no SSE support, so fetch/EventSource consumers negotiate device-bound, one-use WebSocket stream tickets preserving original status/headers and POST body/cursor. Header acquisition, disconnect, revocation, oversized frames and stalled readers are bounded/cancellable. Unknown third-party consumers bypassing window.fetch get an explicit incompatibility rather than false success.

## Checks

`python -m unittest test_gateway -v` runs isolated real HTTP/WS sockets; `node --test test_bootstrap.cjs` exercises actual transport/connection JavaScript. Current native/browser/HTTPS/lifecycle evidence belongs to the publication report, not an unconditional product guarantee. Physical phone installation and all native platforms require separate testing.

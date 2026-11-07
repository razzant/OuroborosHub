---
name: phone-pwa
title: Phone access
description: Connect your phone to Ouroboros with a private ten-minute QR and revocable device sessions.
version: 0.2.0
type: extension
plugin_api: "2.0"
entry: plugin.py
permissions: [net, route, widget, subprocess, companion_process]
dependencies: ["aiohttp==3.13.2", "qrcode==8.2"]
companion_processes:
  - name: phone_gateway
    command: [python3, companion.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 2
ui_tab:
  tab_id: connect
  title: Phone access
  icon: "▣"
  render:
    kind: module
    entry: widget.js
    start: auto
    appearance: host
    height: 680
    span: 2
when_to_use: Connect a phone, create or revoke a connection QR, or manage phone access to the running Ouroboros.
model_experience:
  what_model_sees: No model calls or tools. The owner manages phone access in Widgets; paired devices use the existing Ouroboros interface.
  token_effect: Only this manifest metadata; no idle model traffic.
---

# Phone access

On this screen you see whether your PC and tunnel are available, create a private QR, and revoke a device. Open **Widgets → Phone access** after enabling. Packages install in the ordinary per-skill environment; first start downloads an exact SHA-256-pinned Cloudflare executable into this skill's private state. No personal Downloads path or OS-autostart is used.

The host supervises the companion for the enabled skill. It binds two loopback listeners; only the public gateway is tunneled. Disable/unload/Panic stops the companion and its owned tunnel. The widget is disposable, not the process owner. No core, selected interface, Telegram or other agent is modified.

One QR admits repeated independent browser/app connections for 600 seconds, until revoked or this gateway restarts. Expiry/revocation blocks NEW sessions only. Existing device cookies last 30 days or explicit device revocation. A paired device has FULL owner powers; this is not a restricted account. Keep QR/link private. Cloudflare terminates HTTPS and transports the traffic: this is not end-to-end encryption against the tunnel provider.

The PC and Ouroboros must remain on. A restarted Quick Tunnel changes hostname: create a fresh QR and reinstall that address on the phone. An old icon cannot discover the new hostname. No domain purchase is needed. Connection directions for Safari/iOS 26 and Android appear in the widget and phone page; physical installation/keyboard behavior is not certified by browser emulation.

State and device hashes live only in the dedicated skill state directory. The management listener has an independent bearer capability, never exposed to the phone. The extension's own management-route prefix is excluded from phone forwarding; paired devices otherwise retain ordinary owner control. Revocation cancels gateway I/O but cannot undo already accepted core actions.

The gateway reads the selected UI and overlays installation metadata and an explicitly version-bound response-only chat backport; it writes no upstream files. Unsupported UI bindings report incompatibility before readiness. No service worker/offline chat cache is installed; existing UI preview/draft behavior is preserved. SSE is bridged over device-bound WebSockets because Quick Tunnel does not support SSE. See README.md for limitations and SOURCES.md for reused MIT tunnel primitives.

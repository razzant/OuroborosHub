# Email Presence

Version 0.9.0 connects multiple Gmail API and IMAP/SMTP mailboxes, including
Yandex and Proton Mail Bridge. Each account has its own credentials, inbox,
outbox and receipts; sender addresses are separate from mailbox identity.
The Email accounts widget manages connections and shows queue counts and the
latest delivery receipts, including retained work after a disconnect.

Accounts are usable through ordinary tools without a Place. Connecting a Place
adds automatic intake; its Silent/Paused controls still apply to that account.
Existing EMAIL_* configurations keep their original identity and mail history.
Known thread replies preserve RFC threading, and transient Host failures retry
stored paid output without generating it again. Unknown provider acceptance is
never automatically resent. Sender authentication remains explicitly unverified.

See [SKILL.md](SKILL.md) for setup, scopes, TLS, credential storage, compatibility
and residuals, and [CHANGELOG.md](CHANGELOG.md) for release changes. Google mail
and calendar use separate consents; Proton Bridge must be running locally.

Run offline tests from this directory with an isolated temporary data root:
`python3 -m pytest -p no:cacheprovider -q -rs tests`.
Set `OUROBOROS_SOURCE_DIR` to the coordinated core checkout for real Host binding
and child-dispatch tests. Set `OUROBOROS_RUN_MAIL_BROWSER=1` as well for the actual
opaque widget and provider-return browser fixtures. These tests simulate provider
responses; they do not qualify real personal OAuth grants or mail delivery.

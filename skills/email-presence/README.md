# Email Presence

Version 0.11.0 connects Gmail, on-premises Exchange (EWS over HTTPS with NTLM),
and IMAP/SMTP mailboxes, including Yandex and Proton Mail Bridge. Each account
has its own connection, inbox, outbox and receipts. On hosts with service
sign-in, Gmail uses the email-presence card (the catalog lists it as Email inbox) for its separate Google consent.
Previously issued Exchange links still work. Mail asks Exchange whether an item
or folder belongs to the selected mailbox before using its native ID. A shared
login can therefore operate on multiple mailboxes without making their links
interchangeable. Addresses that resolve to the same Exchange store are aliases.
The Email accounts widget manages connections and shows queue counts and the
latest delivery receipts, including retained work after a disconnect.

Accounts are usable through ordinary tools without a Place. Connecting a Place
adds automatic intake; its Silent/Paused controls still apply to that account.
Existing EMAIL_* configurations keep their original identity and mail history.
Known thread replies preserve RFC threading, and transient Host failures retry
stored paid output without generating it again. Unknown provider acceptance is
never automatically resent: for Gmail, Mail looks for the mail in its own mailbox
and otherwise keeps it there as one draft, and tells the owner once in the chat,
naming the mail's request_id. Queued Gmail mail, including mail queued before this
version, goes out under its mailbox's current sign-in, and waits while that mailbox
is signed out of Mail; a line about it is posted only while it is still true.
Sender authentication remains explicitly unverified.

Searches return pages: pass `next_page_token` back as `page_token` with the same
account, folder and query or criteria until it comes back empty. IMAP criteria are
raw search tokens, so quote an ASCII operand that contains spaces, for example
`["SUBJECT", "\"New sign-in\""]`. An empty or failed search is not proof that a
message never arrived; widen the criteria (FROM with SINCE/BEFORE), page through
the results and compare non-ASCII subjects in their decoded form before concluding
that mail is missing.

See [SKILL.md](SKILL.md) for setup, scopes, TLS, credential storage, compatibility
and residuals, and [CHANGELOG.md](CHANGELOG.md) for release changes. Google mail
and calendar use separate consents; Proton Bridge must be running locally.

Run offline tests from this directory with an isolated temporary data root:
`python3 -m pytest -p no:cacheprovider -q -rs tests`.
Set `OUROBOROS_SOURCE_DIR` to the coordinated core checkout for real Host binding
and child-dispatch tests. Set `OUROBOROS_RUN_MAIL_BROWSER=1` as well for the actual
opaque widget and provider-return browser fixtures. These tests simulate provider
responses; they do not qualify real personal OAuth grants or mail delivery.

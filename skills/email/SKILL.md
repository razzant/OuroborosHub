---
name: email
version: 0.1.2
title: Email tools
type: extension
description: "Email tools for tasks: read and send mail through IMAP/SMTP. Not a place: to let Ouroboros live in a mailbox use the Email connector (email-presence)."
runtime: python3
entry: plugin.py
plugin_api: "2.0"
timeout_sec: 60
permissions:
  - net
  - tool
  - read_settings
env_from_settings:
  - EMAIL_IMAP_HOST
  - EMAIL_SMTP_HOST
  - EMAIL_USER
  - EMAIL_PASSWORD
  - EMAIL_IMAP_PORT
  - EMAIL_SMTP_PORT
  - EMAIL_DEFAULT_FOLDER
when_to_use: When the user wants to list unread emails, read email message contents, compose and send emails via standard IMAP/SMTP protocols, or test email server connectivity.
tools:
  - name: email_list_unread
    description: List unread email message headers (id, subject, sender, date, flags) in an IMAP folder without marking read.
    parameters:
      folder:
        type: string
        description: IMAP mailbox folder to inspect (defaults to EMAIL_DEFAULT_FOLDER or 'INBOX').
      since:
        type: string
        description: Optional search filter date in format YYYY-MM-DD or DD-Mon-YYYY.
      limit:
        type: integer
        description: Maximum number of unread email summaries to return (default 50, clamp 1-200).
  - name: email_read
    description: Fetch and parse email message content (plain text with HTML fallback) by message ID. Peeks safely by default unless mark_as_read is explicitly true.
    parameters:
      message_id:
        type: string
        description: The positive integer message sequence number on the IMAP server.
      mark_as_read:
        type: boolean
        description: Safe default false (peeks without changing unread flag). Set true to mark as read.
      folder:
        type: string
        description: Folder containing the message.
      max_body_chars:
        type: integer
        description: Maximum characters of body text/HTML to return before truncation.
  - name: email_send
    description: Compose and send an email via authenticated SMTP with optional reply-to threading headers.
    parameters:
      to:
        type: string
        description: Recipient email address or comma-separated list of addresses.
      subject:
        type: string
        description: Email subject line.
      body:
        type: string
        description: Body text of the message.
      reply_to_message_id:
        type: string
        description: Optional Message-ID string to populate In-Reply-To and References headers.
      cc:
        type: string
        description: Optional CC recipient address.
      bcc:
        type: string
        description: Optional BCC recipient address.
      body_type:
        type: string
        description: Content type of the body ('plain' or 'html').
  - name: email_test_connection
    description: Validate configured IMAP and SMTP endpoints, TLS/SSL certificates, and authentication credentials without sending mail.
    parameters:
      check_smtp:
        type: boolean
        description: Whether to test SMTP TLS and auth in addition to IMAP.
---

# Email Extension Skill for Ouroboros

A universal, secure, company-agnostic email extension skill for Ouroboros that enables reading, searching, and sending emails via standard IMAP and SMTP protocols.

## Features & Safe Defaults

- **Zero External Dependencies**: Implemented purely using Python standard library (`imaplib`, `smtplib`, `email`, `ssl`).
- **Read-Only Peeking by Default**: `email_list_unread` and `email_read` inspect messages using `BODY.PEEK[]` and read-only folder selection. Emails are never marked read unless `mark_as_read=True` is explicitly passed.
- **Strict TLS/SSL Security**: Direct SSL on port 993 (IMAP) and port 465 (SMTP), or mandatory STARTTLS on port 587 (SMTP) with certificate verification. Plaintext transmission of credentials or messages is strictly prohibited.
- **Output Bounding & Discipline**: Message bodies are bounded (default 25,000 characters) with explicit truncation notices and metadata flags to prevent context budget overflows.
- **Full MIME & Charset Support**: RFC 2047 header decoding, multipart/alternative, multipart/mixed, UTF-8, Latin-1, Windows-1251, KOI8-R decoding with fallback.
- **Email Threading**: Full support for `In-Reply-To` and `References` headers when replying.
- **Integration Preflight**: Built-in `email_test_connection` tool to verify server reachability and credentials without modifying state or sending mail.

---

## Configuration & Owner Grants

Configure credentials in **Settings -> Secrets & Grants**:

| Setting Key | Description | Example |
|---|---|---|
| `EMAIL_IMAP_HOST` | IMAP server hostname (required) | `imap.gmail.com` |
| `EMAIL_SMTP_HOST` | SMTP server hostname (required) | `smtp.gmail.com` |
| `EMAIL_USER` | Email address / username (required) | `user@example.com` |
| `EMAIL_PASSWORD` | App password or account password (required) | `abcd efgh ijkl mnop` |
| `EMAIL_IMAP_PORT` | IMAP port (optional, default 993) | `993` |
| `EMAIL_SMTP_PORT` | SMTP port (optional, default 587) | `587` |
| `EMAIL_DEFAULT_FOLDER`| Default mailbox folder (optional, default `INBOX`) | `INBOX` |

---

## Provider Setup Guides

### 1. Gmail / Google Workspace (App Password)
1. Enable 2-Step Verification in your Google Account security settings.
2. Go to **Security -> 2-Step Verification -> App passwords**.
3. Create an App password (e.g. name it "Ouroboros Email").
4. Copy the generated 16-character password into `EMAIL_PASSWORD`.
5. Settings:
   - `EMAIL_IMAP_HOST`: `imap.gmail.com` (Port 993)
   - `EMAIL_SMTP_HOST`: `smtp.gmail.com` (Port 587)
   - `EMAIL_USER`: your full `@gmail.com` or Google Workspace email.

### 2. Yandex Mail (App Password)
1. Open **Yandex ID -> Security -> App passwords**.
2. Create a password of type "Mail".
3. Settings:
   - `EMAIL_IMAP_HOST`: `imap.yandex.ru` (Port 993)
   - `EMAIL_SMTP_HOST`: `smtp.yandex.ru` (Port 465)
   - `EMAIL_USER`: your Yandex username or `@yandex.ru` address.

### 3. Apple iCloud Mail
1. Generate an app-specific password at `appleid.apple.com`.
2. Settings:
   - `EMAIL_IMAP_HOST`: `imap.mail.me.com` (Port 993)
   - `EMAIL_SMTP_HOST`: `smtp.mail.me.com` (Port 587)
   - `EMAIL_USER`: your `@icloud.com` email address.

### 4. Generic / Corporate / Self-Hosted IMAP & SMTP (Exchange, Postfix, Dovecot)
- Standard SSL IMAP: Host `mail.yourdomain.com`, Port `993`.
- Standard Submission SMTP: Host `mail.yourdomain.com`, Port `587` (STARTTLS) or `465` (SSL).

---

## Tool Reference

### `email_test_connection(check_smtp=True)`
Validates IMAP and SMTP server reachability, TLS negotiation, and credentials without modifying state or sending mail.

### `email_list_unread(folder="INBOX", since=None, limit=50)`
Lists unread email headers in `folder`. Does not clear unread flags.
- `folder`: Mailbox folder name (default: `INBOX`).
- `since`: Optional date string in `YYYY-MM-DD` or `DD-Mon-YYYY` format.
- `limit`: Maximum number of message headers to return (1 to 200).

### `email_read(message_id, mark_as_read=False, folder="INBOX", max_body_chars=25000)`
Fetches and decodes email content and attachment metadata.
- `message_id`: Message ID / sequence number.
- `mark_as_read`: Boolean (default `false`). If `false`, peeks without changing flags.
- `folder`: Folder containing the email (default: `INBOX`).
- `max_body_chars`: Character truncation ceiling for body text.

### `email_send(to, subject, body, reply_to_message_id=None, cc=None, bcc=None, body_type="plain")`
Composes and sends an email via authenticated SMTP.
- `to`: Recipient email address or list of addresses.
- `subject`: Subject line.
- `body`: Message body text.
- `reply_to_message_id`: Optional Message-ID to set `In-Reply-To` and `References` headers.
- `cc`, `bcc`: Optional CC / BCC recipient addresses.
- `body_type`: `"plain"` or `"html"`.

---

## Roadmap & Future Work
- **OAuth 2.0 / XOAUTH2**: Integration with Microsoft 365 and Google Workspace interactive token refresh.
- **Attachment Extraction**: Saving incoming attachment files to task artifact storage on demand.
- **Folder Management**: Creating, moving, and archiving messages across folders.

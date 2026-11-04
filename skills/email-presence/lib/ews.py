"""Mail operations for an explicitly configured Exchange Server EWS mailbox."""
from __future__ import annotations

import base64
import binascii
import hashlib
from email.utils import getaddresses, parsedate_to_datetime

from .client import MailClient
from .ews_operations import EwsOperations
from .ews_wire import EwsError, EwsWire, NS, ews_timestamp, m, t
from .mime import _html_to_text, _message_ids, decode_header_value, parse_message


# These are EWS distinguished folder IDs, not localized mailbox display names.
_KNOWN = {'inbox': 'inbox', 'draft': 'drafts', 'drafts': 'drafts', 'sent': 'sentitems',
          'sentitems': 'sentitems', 'trash': 'deleteditems', 'deleteditems': 'deleteditems',
          'junk': 'junkemail', 'spam': 'junkemail', 'junkemail': 'junkemail',
          'outbox': 'outbox', 'msgfolderroot': 'msgfolderroot'}


_METADATA = ('item:Subject', 'item:DateTimeReceived', 'item:ParentFolderId', 'item:InReplyTo',
             'item:Flag', 'message:From', 'message:InternetMessageId', 'message:References', 'message:IsRead')


def _shape(kind, properties=(), *, mime=False, text_body=False):
    values = [t('BaseShape', text='IdOnly')]
    if mime:
        values.append(t('IncludeMimeContent', text='true'))
    if text_body:
        values.append(t('BodyType', text='Text'))
    if properties:
        values.append(t('AdditionalProperties', *(t('FieldURI', FieldURI=p) for p in properties)))
    return m(kind + 'Shape', *values)


def _opaque(value):
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('Choose the exact Exchange identifier returned for this account')
    return value


def _child(parent, path):
    node = parent.find(path, NS)
    if node is None:
        raise EwsError('MissingResult')
    return node


def _only(parent, container):
    children = _child(parent, container)
    if len(children) != 1:
        raise EwsError('UnexpectedResultCount')
    return children[0]


def _page(root, offset):
    more = root.get('IncludesLastItemInRange')
    if more in ('true', '1'):
        return ''
    value = root.get('IndexedPagingOffset', '')
    if more not in ('false', '0') or not value.isascii() or not value.isdecimal() or int(value) <= offset:
        raise EwsError('InvalidPageBoundary')
    return value


def _fallback_id(native):
    digest = hashlib.sha256(native.encode()).hexdigest()
    return f'<ews-{digest}@ouroboros.local>'


class EwsClient(EwsOperations, MailClient):
    transport = 'ews'

    def __init__(self, account, *, session=None):
        self.account = account
        super().__init__({'EMAIL_USER': account['address'], 'EMAIL_DEFAULT_SENDER': account.get('sender') or account['address'],
                          'EMAIL_DEFAULT_FOLDER': account.get('folder') or 'INBOX'})
        self.wire = EwsWire(account['config'], session=session)

    def close(self):
        self.wire.close()

    def _call(self, operation):
        return self.wire.call(operation)

    @staticmethod
    def native_id(message_ref):
        if not isinstance(message_ref, str) or not message_ref.startswith('ews:'):
            raise ValueError('Use the Exchange message_ref from email_search or email_read')
        return _opaque(message_ref[4:])

    def _folder_ref(self, native):
        return f'ews-folder:{self.account["id"]}:{_opaque(native)}'

    def _folder(self, value):
        value = str(value or 'INBOX')
        known = _KNOWN.get(value.lower())
        if known:
            return t('DistinguishedFolderId', t('Mailbox', t('EmailAddress', text=self.account['address'])), Id=known)
        if value.startswith('ews-folder:'):
            prefix = f'ews-folder:{self.account["id"]}:'
            if not value.startswith(prefix):
                raise ValueError('This Exchange folder handle belongs to a different account')
            return t('FolderId', Id=_opaque(value[len(prefix):]))
        matches = [entry for entry in self._folders() if entry['name'] == value]
        if len(matches) != 1:
            raise ValueError('Choose an exact Exchange folder id or a unique name from email_folders')
        return self._folder(matches[0]['id'])

    def _folders(self):
        offset = 0
        rows = []
        while True:
            reply = self._call(m('FindFolder', _shape('Folder', ('folder:DisplayName', 'folder:ParentFolderId', 'folder:FolderClass')),
                                 m('IndexedPageFolderView', MaxEntriesReturned=200, Offset=offset, BasePoint='Beginning'),
                                 m('ParentFolderIds', self._folder('msgfolderroot')), Traversal='Deep'))
            root = _child(reply, 'm:RootFolder')
            for item in _child(root, 't:Folders'):
                if item.tag not in {f'{{{NS["t"]}}}Folder', f'{{{NS["t"]}}}SearchFolder'}:
                    continue
                folder_class = item.findtext('t:FolderClass', '', NS)
                if folder_class and not folder_class.lower().startswith('ipf.note'):
                    continue
                native = _opaque(_child(item, 't:FolderId').get('Id'))
                parent = item.find('t:ParentFolderId', NS)
                rows.append({'id': self._folder_ref(native), 'name': item.findtext('t:DisplayName', '', NS),
                             'parent_id': self._folder_ref(parent.get('Id')) if parent is not None and parent.get('Id') else ''})
            token = _page(root, offset)
            if not token:
                return rows
            offset = int(token)

    def _folder_info(self, value):
        reply = self._call(m('GetFolder', _shape('Folder', ('folder:TotalCount',)),
                             m('FolderIds', self._folder(value))))
        folder = _only(reply, 'm:Folders')
        result = {'id': _opaque(_child(folder, 't:FolderId').get('Id'))}
        try:
            result['server_time'] = parsedate_to_datetime(self.wire.last_date).timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
        return result

    def test_connection(self):
        info = self._folder_info('INBOX')
        return {'ok': True, 'provider': 'ews', 'address': self.account['address'],
                **({'server_time': info['server_time']} if 'server_time' in info else {})}

    def _facts(self, item):
        native = _opaque(_child(item, 't:ItemId').get('Id'))
        parent = item.find('t:ParentFolderId', NS)
        flags = []
        if item.findtext('t:IsRead', namespaces=NS) == 'true':
            flags.append(r'\Seen')
        if item.findtext('t:Flag/t:FlagStatus', namespaces=NS) == 'Flagged':
            flags.append(r'\Flagged')
        result = {'provider_id': native, 'provider_event_key': 'ews:' + native, 'message_ref': 'ews:' + native,
                  'flags': flags, 'folder': self._folder_ref(parent.get('Id')) if parent is not None and parent.get('Id') else ''}
        received = item.findtext('t:DateTimeReceived', namespaces=NS)
        if received:
            result['internal_date'] = ews_timestamp(received)
        return result

    def _metadata(self, item):
        facts = self._facts(item)
        return {**facts, 'subject': item.findtext('t:Subject', '', NS), 'sender': item.findtext('t:From/t:Mailbox/t:EmailAddress', '', NS),
                'sender_name': item.findtext('t:From/t:Mailbox/t:Name', '', NS),
                'message_id': item.findtext('t:InternetMessageId', '', NS) or _fallback_id(facts['provider_id']),
                'references': _message_ids(item.findtext('t:References', '', NS)), 'in_reply_to': item.findtext('t:InReplyTo', '', NS),
                'date': item.findtext('t:DateTimeReceived', '', NS)}

    def _find(self, folder, limit, offset, *, restriction=None, query=''):
        shape = _shape('Item', _METADATA)
        op = m('FindItem', shape, m('IndexedPageItemView', MaxEntriesReturned=limit, Offset=offset, BasePoint='Beginning'),
               Traversal='Shallow')
        if restriction is not None:
            op.append(m('Restriction', restriction))
        op.append(m('SortOrder', t('FieldOrder', t('FieldURI', FieldURI='item:DateTimeReceived'), Order='Descending')))
        op.append(m('ParentFolderIds', self._folder(folder)))
        if query:
            op.append(m('QueryString', text=query))
        root = _child(self._call(op), 'm:RootFolder')
        results = [self._metadata(item) for item in _child(root, 't:Items')]
        return {'folder': folder, 'messages': results, 'next_page_token': _page(root, offset),
                'result_size_estimate': int(root.get('TotalItemsInView')) if root.get('TotalItemsInView') else None}

    def search(self, *, folder='INBOX', criteria=None, query='', limit=50, page_token=''):
        if criteria and criteria != ['ALL']:
            raise ValueError('Use query with Exchange search syntax; IMAP criteria belong to IMAP accounts')
        token = str(page_token or '0')
        if not token.isascii() or not token.isdecimal():
            raise ValueError('Use the continuation page_token from Exchange search')
        return self._find(folder, max(1, min(200, int(limit))), int(token), query=query)

    def _get_item(self, native, shape):
        reply = self._call(m('GetItem', shape, m('ItemIds', t('ItemId', Id=native))))
        item = _only(reply, 'm:Items')
        if _child(item, 't:ItemId').get('Id') != native:
            raise EwsError('ItemIdentityMismatch')
        return item

    def _structured_read(self, native):
        # A GetItem MIME conversion error says Exchange could not *generate*
        # MIME. The original stored properties are a distinct readable format.
        fields = _METADATA + ('item:Body', 'item:InternetMessageHeaders', 'item:Attachments',
                              'message:ToRecipients', 'message:CcRecipients', 'message:ReplyTo')
        item = self._get_item(native, _shape('Item', fields, text_body=True))
        msg = self._metadata(item)
        headers = {}
        for row in item.findall('t:InternetMessageHeaders/t:InternetMessageHeader', NS):
            name = (row.get('HeaderName') or '').casefold()
            if name:
                headers.setdefault(name, []).append(decode_header_value(row.text or ''))

        def addresses(prop, header):
            values = [(box.findtext('t:Name', '', NS), box.findtext('t:EmailAddress', '', NS))
                      for box in item.findall('t:' + prop + '/t:Mailbox', NS)
                      if box.findtext('t:RoutingType', 'SMTP', NS).upper() == 'SMTP']
            # Native recipients can mix SMTP addresses and Exchange legacy DNs.
            # Prefer a supplied RFC list just as the normal MIME reader does.
            rfc = [(name, address) for name, address in getaddresses(headers.get(header, [])) if '@' in address]
            return rfc or [(name, address) for name, address in values if '@' in address]

        sender = addresses('From', 'from')
        msg.update(sender=sender[0][1] if sender else '', sender_name=(sender[0][0] if sender else '') or msg['sender_name'],
                   recipients=[v for _, v in addresses('ToRecipients', 'to')], cc=[v for _, v in addresses('CcRecipients', 'cc')],
                   reply_to=[v for _, v in addresses('ReplyTo', 'reply-to')], headers=headers,
                   message_id=item.findtext('t:InternetMessageId', '', NS) or next(iter(headers.get('message-id', [])), '') or msg['message_id'],
                   in_reply_to=msg['in_reply_to'] or next(iter(headers.get('in-reply-to', [])), ''),
                   references=msg['references'] or _message_ids(' '.join(headers.get('references', []))))
        body = item.find('t:Body', NS)
        msg['body'] = (body.text or '') if body is not None else ''
        if body is not None and body.get('BodyType', '').lower() == 'html':
            msg['body_html'], msg['body'] = msg['body'], _html_to_text(msg['body'])
        msg['source_format'] = 'ews_properties'
        msg['source_note'] = 'Exchange could not generate MIME; raw MIME is unavailable. Attachments are metadata only.'
        if body is None or body.get('IsTruncated') in {'1', 'true'}:
            msg['source_note'] += ' Exchange omitted the body.' if body is None else ' Exchange marked the returned body truncated.'
        msg['attachments'] = []
        for node in item.findall('t:Attachments/*', NS):
            aid = node.find('t:AttachmentId', NS)
            size = node.findtext('t:Size', '', NS)
            msg['attachments'].append({'file_name': node.findtext('t:Name', '', NS), 'mime_type': node.findtext('t:ContentType', '', NS),
                                       'content_available': False, **({'size': int(size)} if size.isdecimal() else {}),
                                       **({'provider_attachment_id': aid.get('Id')} if aid is not None and aid.get('Id') else {})})
        return msg

    def read(self, *, message_ref='', include_attachment_data=False, **_legacy):
        native = self.native_id(message_ref)
        shape = _shape('Item', ('item:DateTimeReceived', 'item:ParentFolderId', 'item:Flag',
                                'message:IsRead', 'message:InternetMessageId'), mime=True)
        try:
            item = self._get_item(native, shape)
        except EwsError as exc:
            if exc.proven and exc.code == 'ErrorMimeContentConversionFailed':
                return self._structured_read(native)
            raise
        facts = self._facts(item)
        try:
            encoded = _child(item, 't:MimeContent').text or ''
            raw = base64.b64decode(''.join(encoded.split()), validate=True)
        except (ValueError, binascii.Error):
            raise EwsError('InvalidMimeContent') from None
        if not raw:
            raise EwsError('MissingMimeContent')
        message = parse_message(raw, uid=0, folder=facts['folder'], include_attachment_data=include_attachment_data)
        message.pop('uid')  # Only intake allocates a local numeric address; public EWS IDs stay opaque.
        provider_mid = item.findtext('t:InternetMessageId', '', NS)
        if provider_mid:
            message['message_id'] = provider_mid
        elif message['message_id'] == f'<sha256-{hashlib.sha256(raw).hexdigest()}@ouroboros.local>':
            message['message_id'] = _fallback_id(native)
        return {**message, **facts}

    def find_message(self, message_id, *, folder='INBOX'):
        restriction = t('IsEqualTo', t('FieldURI', FieldURI='message:InternetMessageId'),
                        t('FieldURIOrConstant', t('Constant', Value=message_id)))
        page = self._find(folder, 1, 0, restriction=restriction)
        if not page['messages']:
            return None
        original = self.read(message_ref=page['messages'][0]['message_ref'])
        return original if original.get('message_id') == message_id else None

    def mark_read(self, *, message_ref='', **_legacy):
        return self.mailbox(action='flags', message_ref=message_ref, flags=[r'\Seen'])

    def mailbox(self, *, action='list', folder=None, destination='', message_ref='', flags=None,
                remove_flags=False, **_other):
        if action == 'list':
            return {'folders': self._folders()}
        if action == 'create':
            if not destination:
                raise ValueError('Supply the name of the new Exchange folder as destination')
            response = self._call(m('CreateFolder', m('ParentFolderId', self._folder(folder or 'msgfolderroot')),
                                    m('Folders', t('Folder', t('DisplayName', text=destination)))))
            node = _only(response, 'm:Folders')
            return {'ok': True, 'id': self._folder_ref(_child(node, 't:FolderId').get('Id')), 'name': destination}
        if action not in {'copy', 'move', 'trash', 'flags'}:
            raise ValueError('Exchange mailbox supports list, create, copy, move, trash and flags')
        native = self.native_id(message_ref)
        if action in {'copy', 'move', 'trash'}:
            if action != 'trash' and not destination:
                raise ValueError('Choose a destination Exchange folder')
            target = self._folder('deleteditems' if action == 'trash' else destination)
            op = 'CopyItem' if action == 'copy' else 'MoveItem'
            result = self._call(m(op, m('ToFolderId', target), m('ItemIds', t('ItemId', Id=native))))
            changed = result.find('m:Items/*/t:ItemId', NS)
            return {'ok': True, **({'message_ref': 'ews:' + _opaque(changed.get('Id'))} if changed is not None else {})}
        updates = []
        for flag in dict.fromkeys(flags or []):
            if flag == r'\Seen':
                uri, value = 'message:IsRead', t('IsRead', text='false' if remove_flags else 'true')
            elif flag == r'\Flagged':
                uri, value = 'item:Flag', t('Flag', t('FlagStatus', text='NotFlagged' if remove_flags else 'Flagged'))
            else:
                raise ValueError('Exchange flags support Seen and Flagged; use trash to move a message to Deleted Items')
            updates.append(t('SetItemField', t('FieldURI', FieldURI=uri), t('Message', value)))
        if not updates:
            raise ValueError('Choose at least one Exchange flag')
        now = self._call(m('GetItem', _shape('Item'), m('ItemIds', t('ItemId', Id=native))))
        current = _child(_only(now, 'm:Items'), 't:ItemId')
        if current.get('Id') != native or not current.get('ChangeKey'):
            raise EwsError('MissingCurrentItemVersion')
        self._call(m('UpdateItem', m('ItemChanges', t('ItemChange', current, t('Updates', *updates))),
                     ConflictResolution='NeverOverwrite', MessageDisposition='SaveOnly', SuppressReadReceipts='true'))
        return {'ok': True}

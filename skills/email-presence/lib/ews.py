"""Mail operations for an explicitly configured Exchange Server EWS mailbox."""
from __future__ import annotations

import base64
import binascii
import hashlib
from email.utils import getaddresses, parsedate_to_datetime

from .client import MailClient
from .ews_operations import EwsOperations
from .ews_wire import EwsError, EwsWire, NS, ews_timestamp, m, t
from .mime import _html_to_text, _message_ids, decode_header_value, parse_message, usable_message_id


# These are EWS distinguished folder IDs, not localized mailbox display names.
_KNOWN = {'inbox': 'inbox', 'draft': 'drafts', 'drafts': 'drafts', 'sent': 'sentitems',
          'sentitems': 'sentitems', 'trash': 'deleteditems', 'deleteditems': 'deleteditems',
          'junk': 'junkemail', 'spam': 'junkemail', 'junkemail': 'junkemail',
          'outbox': 'outbox', 'msgfolderroot': 'msgfolderroot'}


_METADATA = ('item:Subject', 'item:DateTimeReceived', 'item:ParentFolderId', 'item:InReplyTo',
             'item:Flag', 'message:From', 'message:InternetMessageId', 'message:References', 'message:IsRead')


def _shape(kind, properties=(), *, mime=False, text_body=False, store_key=False):
    values = [t('BaseShape', text='IdOnly')]
    if mime:
        values.append(t('IncludeMimeContent', text='true'))
    if text_body:
        values.append(t('BodyType', text='Text'))
    fields = [t('FieldURI', FieldURI=p) for p in properties]
    if store_key:  # PidTagStoreRecordKey: binary-comparable identity of an object's message store.
        fields.append(t('ExtendedFieldURI', PropertyTag='0x0FFA', PropertyType='Binary'))
    if fields:
        values.append(t('AdditionalProperties', *fields))
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
            native = _opaque(value[len(prefix):])
            self._checked_native('Folder', native)
            return t('FolderId', Id=native)
        matches = [entry for entry in self._folders() if entry['name'] == value]
        if len(matches) != 1:
            raise ValueError('Choose an exact Exchange folder id or a unique name from email_folders')
        # FindFolder was rooted in the explicit mailbox; this ID has server provenance already.
        return t('FolderId', Id=matches[0]['id'].removeprefix(f'ews-folder:{self.account["id"]}:'))

    def _folders(self):
        return list(self._folder_rows())

    def _folder_rows(self):
        offset = 0
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
                yield {'id': self._folder_ref(native), 'name': item.findtext('t:DisplayName', '', NS),
                       'parent_id': self._folder_ref(parent.get('Id')) if parent is not None and parent.get('Id') else ''}
            token = _page(root, offset)
            if not token:
                return
            offset = int(token)

    def _scope_record(self, kind, ref):
        """Read identity only: a possibly foreign object's content must never be requested as proof."""
        def fetch(store_key):
            shape = _shape(kind, (kind.lower() + ':ParentFolderId',), store_key=store_key)
            return _only(self._call(m('Get' + kind, shape, m(kind + 'Ids', ref))), 'm:' + kind + 's')
        try:
            node = fetch(True)
        except EwsError as exc:
            if not exc.proven or exc.code not in {'ErrorInvalidPropertyRequest', 'ErrorInvalidPropertyForOperation'}:
                raise
            node = fetch(False)  # Server rejected this projection. Its scoped folder tree can still prove ownership.
        identity = _opaque(_child(node, 't:' + kind + 'Id').get('Id'))
        if ref.tag == f'{{{NS["t"]}}}{kind}Id' and ref.get('Id') != identity:
            raise EwsError(kind + 'IdentityMismatch')
        parent_node = node.find('t:ParentFolderId', NS)
        parent = parent_node.get('Id') if parent_node is not None else None
        key = None
        for prop in node.findall('t:ExtendedProperty', NS):
            field = prop.find('t:ExtendedFieldURI', NS)
            if field is not None and field.get('PropertyTag', '').lower() in {'0x0ffa', '0xffa', '4090'}:
                try:
                    if key is not None or field.get('PropertyType') != 'Binary':
                        raise ValueError()
                    key = base64.b64decode(''.join(prop.findtext('t:Value', '', NS).split()), validate=True)
                    if not key:
                        raise ValueError()
                except (ValueError, binascii.Error):
                    raise EwsError('InvalidStoreRecordKey') from None
        return node, identity, parent, key

    def _checked_native(self, kind, native):
        """Verify an unchanged public handle against this mailbox, not its editable account prefix."""
        node, identity, parent, key = self._scope_record(kind, t(kind + 'Id', Id=native))
        folder = identity if kind == 'Folder' else parent
        def unavailable(exc):
            return exc.proven and exc.code in {'ErrorFolderNotFound', 'ErrorAccessDenied'}
        def matches_anchor(name):
            try:
                _, anchor, anchor_parent, anchor_key = self._scope_record('Folder', self._folder(name))
            except EwsError as exc:
                if unavailable(exc):
                    return False
                raise
            if key is not None and anchor_key is not None:
                if key != anchor_key:
                    raise EwsError('MailboxOwnershipMismatch')
                return True
            return bool(folder and folder in (anchor, anchor_parent))

        if matches_anchor('inbox'):
            return node
        # Some stores don't expose the extended property. The actual ID (or item's
        # parent) must then be found beneath a folder explicitly qualified by mailbox.
        if folder:
            try:
                if any(row['id'] == self._folder_ref(folder) for row in self._folder_rows()):
                    return node
            except EwsError as exc:
                if not unavailable(exc):
                    raise
        # A delegate can have access to a single default folder but no Inbox or
        # deep hierarchy. Its explicit resolution is just as authoritative.
        if any(matches_anchor(name) for name in dict.fromkeys(_KNOWN.values()) if name != 'inbox'):
            return node
        raise EwsError('MailboxOwnershipUnverified')

    def _resolved_folder(self, folder, defaults):
        """Read the names of a mailbox-qualified folder or an already verified native ID."""
        distinguished = folder.get('Id') if folder.tag == f'{{{NS["t"]}}}DistinguishedFolderId' else ''
        def fetch(*extra):
            shape = _shape('Folder', ('folder:DisplayName', *extra))
            return _only(self._call(m('GetFolder', shape, m('FolderIds', folder))), 'm:Folders')
        if distinguished:
            node = fetch()
        else:
            try:
                node = fetch('folder:DistinguishedFolderId')
            except EwsError as exc:
                if not exc.proven or exc.code not in {'ErrorInvalidPropertyRequest', 'ErrorInvalidPropertyForOperation'}:
                    raise
                node = fetch()  # Older servers can reject the optional property itself.
            distinguished = node.findtext('t:DistinguishedFolderId', '', NS)
        native = _opaque(_child(node, 't:FolderId').get('Id'))
        if folder.tag == f'{{{NS["t"]}}}FolderId' and folder.get('Id') != native:
            raise EwsError('FolderIdentityMismatch')
        name = node.findtext('t:DisplayName', '', NS)
        if not name:
            raise EwsError('MissingFolderDisplayName')
        if not distinguished and not defaults:
            # A localized display name cannot tell us that this is e.g. Deleted Items.
            # Resolve the supported defaults once for this dispatch, including inaccessible
            # entries; never carry these IDs across tool calls, accounts or retries.
            kinds = list(dict.fromkeys(_KNOWN.values()))
            replies = self.wire.read_folder_ids([self._folder(kind) for kind in kinds])
            for kind, reply in zip(kinds, replies):
                defaults[kind] = _opaque(_child(_only(reply, 'm:Folders'), 't:FolderId').get('Id')) if reply is not None else None
        names = sorted({name, *(alias for alias, kind in _KNOWN.items()
                                if kind == distinguished or (not distinguished and defaults.get(kind) == native))})
        return t('FolderId', Id=native), self._folder_ref(native), names

    def draft_destination(self, selection):
        selected, handle, names = self._resolved_folder(self._folder(selection), {})
        return handle, names, lambda **content: self._save_draft(selected, **content)

    def _item_parent(self, checked_item, defaults):
        native = _opaque(_child(checked_item, 't:ParentFolderId').get('Id'))
        return self._resolved_folder(t('FolderId', Id=native), defaults)

    @staticmethod
    def _prepare_folders(prepare, *, new_name='', **folders):
        names = {new_name} if new_name else set()
        targets = {}
        for key, (_, handle, aliases) in folders.items():
            targets[key] = handle
            names.update((handle, *aliases))
        return prepare({**targets, 'folder_names': sorted(names)})

    def _folder_info(self, value):
        folder = self._folder(value)
        if folder.tag != f'{{{NS["t"]}}}FolderId':
            reply = self._call(m('GetFolder', _shape('Folder', ('folder:TotalCount',)), m('FolderIds', folder)))
            folder = _child(_only(reply, 'm:Folders'), 't:FolderId')
        result = {'id': _opaque(folder.get('Id'))}
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
                'message_id': usable_message_id(item.findtext('t:InternetMessageId', '', NS)) or _fallback_id(facts['provider_id']),
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
        raw_mid = None
        for row in item.findall('t:InternetMessageHeaders/t:InternetMessageHeader', NS):
            name = (row.get('HeaderName') or '').casefold()
            if name == 'message-id' and raw_mid is None:
                raw_mid = row.text
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
                   message_id=usable_message_id(item.findtext('t:InternetMessageId', '', NS))
                              or usable_message_id(raw_mid) or msg['message_id'],
                   in_reply_to=msg['in_reply_to'] or next(iter(headers.get('in-reply-to', [])), ''),
                   # The sender's Date, as the MIME reader gives it; the receipt stays internal_date.
                   date=next(iter(headers.get('date', [])), ''),
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
        self._checked_native('Item', self.native_id(message_ref))
        return self._read_scoped(message_ref=message_ref, include_attachment_data=include_attachment_data)

    def _read_scoped(self, *, message_ref, include_attachment_data=False):
        # Only an already checked public ref, or an ID from mailbox-qualified Find/Sync, enters here.
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
        message = parse_message(raw, uid=0, folder=facts['folder'], include_attachment_data=include_attachment_data,
                                missing_id=_fallback_id(native))
        message.pop('uid')  # Only intake allocates a local numeric address; public EWS IDs stay opaque.
        provider_mid = usable_message_id(item.findtext('t:InternetMessageId', '', NS))
        if provider_mid:
            message['message_id'] = provider_mid
        return {**message, **facts}

    def find_message(self, message_id, *, folder='INBOX'):
        restriction = t('IsEqualTo', t('FieldURI', FieldURI='message:InternetMessageId'),
                        t('FieldURIOrConstant', t('Constant', Value=message_id)))
        page = self._find(folder, 1, 0, restriction=restriction)
        if not page['messages']:
            return None
        original = self._read_scoped(message_ref=page['messages'][0]['message_ref'])
        return original if original.get('message_id') == message_id else None

    def mark_read(self, *, message_ref='', prepare_targets=None, **_legacy):
        return self.mailbox(action='flags', message_ref=message_ref, flags=[r'\Seen'], prepare_targets=prepare_targets)

    def mailbox(self, *, action='list', folder=None, destination='', message_ref='', flags=None,
                remove_flags=False, prepare_targets=None, **_other):
        if action == 'list':
            return {'folders': self._folders()}
        if action == 'create':
            if not destination:
                raise ValueError('Supply the name of the new Exchange folder as destination')
            parent = self._resolved_folder(self._folder(folder or 'msgfolderroot'), {})
            if prepare_targets and (needed := self._prepare_folders(prepare_targets, folder=parent, new_name=destination)):
                return needed
            response = self._call(m('CreateFolder', m('ParentFolderId', parent[0]),
                                    m('Folders', t('Folder', t('DisplayName', text=destination)))))
            node = _only(response, 'm:Folders')
            return {'ok': True, 'id': self._folder_ref(_child(node, 't:FolderId').get('Id')), 'name': destination}
        if action not in {'copy', 'move', 'trash', 'flags'}:
            raise ValueError('Exchange mailbox supports list, create, copy, move, trash and flags')
        native = self.native_id(message_ref)
        if action in {'copy', 'move', 'trash'}:
            if action != 'trash' and not destination:
                raise ValueError('Choose a destination Exchange folder')
            defaults = {}
            target = self._resolved_folder(self._folder('deleteditems' if action == 'trash' else destination), defaults)
            item = self._checked_native('Item', native)
            if prepare_targets:
                source = {} if action == 'copy' else {'folder': self._item_parent(item, defaults)}
                if needed := self._prepare_folders(prepare_targets, destination=target, **source):
                    return needed
            op = 'CopyItem' if action == 'copy' else 'MoveItem'
            result = self._call(m(op, m('ToFolderId', target[0]), m('ItemIds', t('ItemId', Id=native))))
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
        item = self._checked_native('Item', native)
        current = _child(item, 't:ItemId')
        if current.get('Id') != native or not current.get('ChangeKey'):
            raise EwsError('MissingCurrentItemVersion')
        if prepare_targets and (needed := self._prepare_folders(prepare_targets, folder=self._item_parent(item, {}))):
            return needed
        self._call(m('UpdateItem', m('ItemChanges', t('ItemChange', current, t('Updates', *updates))),
                     ConflictResolution='NeverOverwrite', MessageDisposition='SaveOnly', SuppressReadReceipts='true'))
        return {'ok': True}

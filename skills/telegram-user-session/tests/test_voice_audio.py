"""Voice messages and audio files reach the Host as original staged bytes with a typed suffix."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

from telethon import types as T

import test_participant
from userbot.normalize import canonical_text, media_descriptor, staged_suffix

NOW = datetime.now(timezone.utc)
OGG = b'OggS\x00\x02' + b'\x00' * 40 + b'voice-bytes'


def _message(mid, mime, attributes, *, text='', size=None):
    doc = T.Document(id=100 + mid, access_hash=9, file_reference=b'f', date=NOW, mime_type=mime,
                     size=size if size is not None else len(OGG), dc_id=1, attributes=attributes)
    return T.Message(id=mid, peer_id=T.PeerUser(809), date=NOW, message=text, from_id=T.PeerUser(809),
                     media=T.MessageMediaDocument(document=doc))


def _voice(mid=10, mime='audio/ogg', **kw):
    return _message(mid, mime, [T.DocumentAttributeAudio(duration=4, voice=True)], **kw)


def _audio(mid, mime, name=None):
    attributes = [T.DocumentAttributeAudio(duration=200, voice=False, title='Talk')]
    if name:
        attributes.append(T.DocumentAttributeFilename(name))
    return _message(mid, mime, attributes)


class SuffixTests(unittest.TestCase):
    def test_suffix_follows_the_declared_type_not_the_host_mime_table(self):
        cases = [(_voice(), '.ogg'), (_voice(mime='audio/ogg; codecs=opus'), '.ogg'),
                 (_audio(11, 'audio/mpeg'), '.mp3'), (_audio(12, 'audio/x-m4a'), '.m4a'),
                 (_audio(13, 'audio/mp4', 'memo.mp4'), '.m4a'), (_audio(14, 'audio/x-weird', 'take.Wv'), '.wv'),
                 (_audio(15, 'audio/x-weird', 'take'), '.bin'), (_audio(16, 'audio/x-weird', 'a.<bad>'), '.bin')]
        for message, expected in cases:
            with self.subTest(mime=message.media.document.mime_type):
                self.assertEqual(staged_suffix(message, media_descriptor(message)), expected)

    def test_other_media_keep_telethon_suffix(self):
        sticker = _message(20, 'image/webp', [T.DocumentAttributeSticker('x', T.InputStickerSetEmpty())])
        self.assertEqual(staged_suffix(sticker, media_descriptor(sticker)), sticker.file.ext)

    def test_text_is_a_label_never_a_transcript(self):
        self.assertEqual(canonical_text(_voice()), '[Telegram voice message]')
        self.assertEqual(canonical_text(_audio(11, 'audio/mpeg')), '[Telegram audio: Talk]')


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    # The participant fixture only; its own tests stay in test_participant.
    setUp = test_participant.Tests.setUp
    tearDown = test_participant.Tests.tearDown

    async def _capture(self, message, data=OGG):
        async def download(m, file):
            Path(file).write_bytes(data)
        self.client.download_media = download
        event = NS(message=message, chat_id=809, sender_id=809, raw_text=message.message, is_private=True,
                   get_sender=lambda: self.client.get_entity(809), get_chat=lambda: self.client.get_entity(809))
        await self.e.capture(event)
        row = self.j.db.execute('select event,files from inbox order by seq desc limit 1').fetchone()
        return json.loads(row['event']), json.loads(row['files'])

    async def test_voice_is_staged_unchanged_with_ogg_suffix_and_offered_to_the_host(self):
        event, files = await self._capture(_voice())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith('.ogg'))
        self.assertEqual(Path(files[0]).read_bytes(), OGG)
        media = event['message']['media']
        self.assertEqual((media['kind'], media['mime'], media['status']), ('voice', 'audio/ogg', 'staged'))
        self.assertEqual(event['text'], '[Telegram voice message]')
        self.assertEqual(event['facts'], ['any_message', 'direct_message'])
        requests = []

        def host(method, path, body=None):
            requests.append((path, body))
            return 200, {'ok': True, 'status': 'completed', 'outcome': 'silent', 'text': ''}
        self.host.call = host
        await self.e.incoming_once()
        turn = [body for path, body in requests if path == '/presence/turn'][0]
        self.assertEqual(turn['staged_files'], files)

    async def test_m4a_without_a_name_is_not_staged_as_bin(self):
        m4a = b'\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41' + b'\x00' * 32
        _event, files = await self._capture(_audio(12, 'audio/x-m4a'), m4a)
        self.assertTrue(files[0].endswith('.m4a'), files)

    async def test_oversized_recording_keeps_reference_and_stages_nothing(self):
        event, files = await self._capture(_voice(size=33 * 1024 * 1024))
        self.assertEqual(files, [])
        self.assertEqual(event['message']['media']['status'], 'too_large')
        self.assertEqual(event['text'], '[Telegram voice message]')


if __name__ == '__main__':
    unittest.main()

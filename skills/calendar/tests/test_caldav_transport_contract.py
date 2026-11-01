"""Credential origin is checked before transport, including redirects; no network."""
import io
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

SKILL = str(Path(__file__).resolve().parents[1])
if SKILL not in sys.path:
    sys.path.insert(0, SKILL)

from providers import YandexAdapter
from ops import ProviderError


class Response(io.BytesIO):
    status = 200
    headers = {}


class CalDAVTransportContractTests(unittest.TestCase):
    def transport(self, calls):
        def send(request, timeout):
            calls.append(request)
            return Response(b'ok')
        return send

    def test_foreign_or_credential_bearing_href_never_reaches_transport(self):
        urls = ('https://caldav.yandex.ru@evil.example/x.ics',
                'https://caldav.yandex.ru.evil.example/x.ics',
                'http://caldav.yandex.ru/x.ics',
                'https://user:pass@caldav.yandex.ru/x.ics',
                'https://caldav.yandex.ru:444/x.ics')
        for url in urls:
            with self.subTest(url=url):
                calls = []
                transport = self.transport(calls)
                with patch('urllib.request.urlopen', side_effect=transport), patch('urllib.request.build_opener') as build:
                    build.return_value.open.side_effect = transport
                    with self.assertRaises(ProviderError):
                        YandexAdapter('fake@example.test', 'fake-password')._request('GET', url)
                self.assertEqual(calls, [])

    def test_exact_https_origin_still_sends_authenticated_request(self):
        for url in ('https://caldav.yandex.ru/calendars/a/x.ics', 'https://CALDAV.YANDEX.RU:443/calendars/a/x.ics'):
            with self.subTest(url=url):
                calls = []
                transport = self.transport(calls)
                with patch('urllib.request.urlopen', side_effect=transport), patch('urllib.request.build_opener') as build:
                    build.return_value.open.side_effect = transport
                    self.assertEqual(YandexAdapter('fake@example.test', 'fake-password')._request('GET', url), (200, {}, 'ok'))
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0].get_header('Authorization').startswith('Basic '))

    def test_redirect_handler_keeps_the_same_credential_origin(self):
        calls = []
        with patch('urllib.request.build_opener') as build, patch('urllib.request.urlopen', side_effect=self.transport(calls)):
            build.return_value.open.side_effect = self.transport(calls)
            YandexAdapter('fake@example.test', 'fake-password')._request('GET', 'https://caldav.yandex.ru/old')
        handlers = [h for h in build.call_args.args if isinstance(h, urllib.request.HTTPRedirectHandler)] if build.called else []
        self.assertEqual(len(handlers), 1)
        original = urllib.request.Request('https://caldav.yandex.ru/old', headers={'Authorization': 'Basic fake'})
        for target in ('https://evil.example/new', 'http://caldav.yandex.ru/new', 'https://caldav.yandex.ru@evil.example/new'):
            with self.subTest(target=target), self.assertRaises(ProviderError):
                handlers[0].redirect_request(original, None, 302, 'Found', {}, target)
        redirect = handlers[0].redirect_request(original, None, 302, 'Found', {}, 'https://caldav.yandex.ru/new')
        self.assertEqual(redirect.full_url, 'https://caldav.yandex.ru/new')

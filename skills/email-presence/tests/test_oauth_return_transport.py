"""Only the local Host return-page probe bypasses unrelated OS proxies."""
import httpx
import pytest

from lib import google_auth


class Store:
    def begin_oauth(self, state, data):
        self.pending = data


@pytest.mark.parametrize('origin,pool', [('http://127.0.0.1:9876/prefix', 'ConnectionPool'),
    ('http://localhost:9876', 'ConnectionPool'), ('http://[::1]:9876', 'ConnectionPool'),
    ('https://public.example.invalid/prefix', 'HTTPProxy')])
def test_return_probe_uses_local_transport_without_changing_public_origin(monkeypatch, origin, pool):
    for name in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:3128')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:3128')
    monkeypatch.setenv('NO_PROXY', '')
    seen = []
    def transport(self, request):
        seen.append((request.method, str(request.url), type(self._pool).__name__))
        return httpx.Response(200, json={}, request=request)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', transport)
    store = Store()
    result = google_auth.begin(store, {'GOOGLE_OAUTH_CLIENT_ID': 'fixture'}, origin=origin)
    expected = origin + google_auth.RETURN_PAGE
    assert seen == [('HEAD', expected, pool)]
    assert result['redirect_uri'] == store.pending['redirect_uri'] == expected
    google_auth._request('POST', 'https://oauth2.googleapis.com/token', data={'grant_type': 'synthetic'})
    assert seen[-1] == ('POST', 'https://oauth2.googleapis.com/token', 'HTTPProxy')


def test_injected_http_transport_keeps_its_own_configuration():
    calls = []
    class Http:
        def request(self, method, url, *, timeout):
            calls.append((method, url, timeout))
            return httpx.Response(200)
    result = google_auth.begin(Store(), {'GOOGLE_OAUTH_CLIENT_ID': 'fixture'}, origin='http://127.0.0.1:9876', http=Http())
    assert calls == [('HEAD', result['redirect_uri'], 15)]

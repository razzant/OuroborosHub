"""SOAP response authenticity and NTLM connection custody, without a live provider."""
import threading
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET

import pytest
import requests

from lib.ews_wire import EwsError, EwsWire, NS, m

CONFIG = {'EWS_ENDPOINT': 'https://exchange.example.invalid/EWS/Exchange.asmx',
          'EMAIL_USER': 'person@example.invalid', 'EMAIL_PASSWORD': 'test-password-only'}


def envelope(body):
    return ('<s:Envelope ' + ' '.join(f'xmlns:{p}="{uri}"' for p, uri in NS.items())
            + f'><s:Body>{body}</s:Body></s:Envelope>')


def response(operation, inner='', *, klass='Success', code='NoError'):
    msg = f'<m:{operation}ResponseMessage ResponseClass="{klass}"><m:ResponseCode>{code}</m:ResponseCode>{inner}</m:{operation}ResponseMessage>'
    return envelope(f'<m:{operation}Response><m:ResponseMessages>{msg}</m:ResponseMessages></m:{operation}Response>')


class FakeResponse:
    def __init__(self, xml, status=200, **headers):
        self.content, self.status_code, self.headers = xml.encode(), status, headers

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.operations = []
        self.closed = False

    def post(self, url, *, data, headers, allow_redirects, verify, timeout):
        assert url == CONFIG['EWS_ENDPOINT'] and verify and not allow_redirects and min(timeout) > 0
        assert CONFIG['EMAIL_PASSWORD'] not in data.decode()
        root = ET.fromstring(data)
        assert root.find('s:Header/t:RequestServerVersion', NS).get('Version') == 'Exchange2013_SP1'
        op = root.find('s:Body', NS)[0]
        assert headers['SOAPAction'] == NS['m'] + '/' + op.tag.split('}')[-1]
        self.operations.append(op)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, FakeResponse) else FakeResponse(result)

    def close(self):
        self.closed = True


@pytest.mark.parametrize(('value', 'code', 'proven'), [
    (FakeResponse('Sensitive HTML from the proxy', 401), 'HttpFailure', False),
    (FakeResponse('Sensitive HTML from the proxy', 302), 'HttpFailure', False),
    (response('DeleteItem'), 'UnexpectedResponse', False),
    (response('GetFolder', klass='Warning', code='ErrorServerBusy'), 'UncertainResponse', False),
    (response('GetFolder', klass='Error', code='ErrorItemNotFound'), 'ErrorItemNotFound', True),
    (response('GetFolder', klass='Error', code='Sensitive HTML from the proxy'), 'UnknownResponse', False),
    (envelope('<m:GetFolderResponse/>'), 'UnexpectedResponse', False),
    ('<!DOCTYPE a [<!ENTITY x "bad">]>' + response('GetFolder'), 'InvalidXml', False),
    (requests.ConnectionError('Sensitive HTML from the proxy'), 'TransportFailure', False),
])
def test_one_attempt_typed_private_failures(value, code, proven):
    session = Session(value)
    wire = EwsWire(CONFIG, session)
    with pytest.raises(EwsError) as exc:
        wire.call(m('GetFolder'))
    assert exc.value.code == code and exc.value.proven is proven
    assert 'Sensitive' not in str(exc.value)
    assert len(session.operations) == 1


def test_classified_soap_fault_and_backoff():
    value = envelope('<s:Fault><faultcode>s:Server</faultcode><detail><e:ResponseCode>ErrorServerBusy</e:ResponseCode>'
                     '<e:MessageXml><t:Value Name="BackOffMilliseconds">2500</t:Value></e:MessageXml></detail></s:Fault>')
    with pytest.raises(EwsError) as exc:
        EwsWire(CONFIG, Session(FakeResponse(value, 500))).call(m('FindFolder'))
    assert (exc.value.code, exc.value.retry_after, exc.value.proven) == ('ErrorServerBusy', 2.5, True)


def test_each_account_owns_its_session_and_ntlm_calls_are_serial():
    entered, release = threading.Event(), threading.Event()
    class Pausing(Session):
        def post(self, *args, **kwargs):
            assert not getattr(self, 'in_call', False)
            self.in_call = True
            entered.set()
            assert release.wait(4)
            try:
                return super().post(*args, **kwargs)
            finally:
                self.in_call = False
    shared = Pausing(response('GetFolder'), response('GetFolder'))
    other = Session(response('GetFolder'))
    a, b = EwsWire(CONFIG, shared), EwsWire({**CONFIG, 'EMAIL_USER': 'other'}, other)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(a.call, m('GetFolder'))
        assert entered.wait(4)
        second = pool.submit(a.call, m('GetFolder'))
        assert b.call(m('GetFolder')).get('ResponseClass') == 'Success'
        release.set()
        assert all(work.result().get('ResponseClass') == 'Success' for work in (first, second))
    a.close()
    assert shared.closed and not other.closed and a.session.auth is not b.session.auth


def test_real_session_has_no_application_retries():
    wire = EwsWire(CONFIG)
    try:
        assert wire.session.get_adapter(CONFIG['EWS_ENDPOINT']).max_retries.total == 0
    finally:
        wire.close()

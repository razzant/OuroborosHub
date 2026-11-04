"""An account-owned HTTPS/NTLM session and single-operation EWS SOAP contract."""
from __future__ import annotations

import re
import threading
from datetime import datetime
from xml.etree import ElementTree as ET

import requests
from defusedxml import ElementTree as SafeXML
from defusedxml.common import DefusedXmlException
from requests_ntlm import HttpNtlmAuth

from .accounts import _ews_endpoint

NS = {'s': 'http://schemas.xmlsoap.org/soap/envelope/',
      'm': 'http://schemas.microsoft.com/exchange/services/2006/messages',
      't': 'http://schemas.microsoft.com/exchange/services/2006/types',
      'e': 'http://schemas.microsoft.com/exchange/services/2006/errors'}


def _element(ns, tag, children, text, attrs):
    element = ET.Element(f'{{{NS[ns]}}}{tag}', {k: str(v) for k, v in attrs.items()})
    element.extend(children)
    if text is not None:
        element.text = str(text)
    return element


def m(tag, *children, text=None, **attrs):
    return _element('m', tag, children, text, attrs)


def t(tag, *children, text=None, **attrs):
    return _element('t', tag, children, text, attrs)


class EwsError(RuntimeError):
    """Only a typed protocol code, never a server's arbitrary message or private request."""
    def __init__(self, code, *, http_status=None, retry_after=None, proven=False):
        self.code, self.http_status = code, http_status
        self.retry_after, self.proven = retry_after, proven
        super().__init__(f'Exchange: {code}' + (f' (HTTP {http_status})' if http_status else ''))


def ews_timestamp(value):
    """Parse zoned EWS XML dates, including .NET's seven fractional digits on Python 3.10."""
    try:
        # Python 3.10 fromisoformat accepts only three or six fractional digits.
        stamp = re.sub(r"(\.\d+)(?=Z$|[+-]\d\d:\d\d$)", lambda match: match[0][:7].ljust(7, "0"), value)
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.utcoffset() is not None:
            return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    raise EwsError("InvalidTimestamp")


def _code(value):
    return value if re.fullmatch(r'Error[A-Z][A-Za-z0-9]{0,95}', value or '') else 'UnknownResponse'


def _delay(response, node):
    for child in node.iter():
        if child.tag == f'{{{NS["t"]}}}Value' and child.get('Name') == 'BackOffMilliseconds':
            if (child.text or '').isascii() and (child.text or '').isdigit():
                return int(child.text) / 1000
    header = response.headers.get('Retry-After', '')
    return int(header) if header.isascii() and header.isdigit() else None


class EwsWire:
    def __init__(self, config, session=None):
        self.endpoint = _ews_endpoint(config['EWS_ENDPOINT'])
        self.session = session if session is not None else requests.Session()
        self.session.auth = HttpNtlmAuth(config['EMAIL_USER'], config['EMAIL_PASSWORD'])
        self.lock = threading.Lock()  # One NTLM connection pool for this account, shared by its workers.
        self._local = threading.local()

    @property
    def last_date(self):
        """HTTP Date from this thread's last response, for the first sync's activation boundary."""
        return getattr(self._local, 'date', None)

    def close(self):
        with self.lock:
            self.session.close()

    def call(self, operation):
        name = operation.tag.removeprefix(f'{{{NS["m"]}}}')
        envelope = ET.Element(f'{{{NS["s"]}}}Envelope')
        ET.SubElement(envelope, f'{{{NS["s"]}}}Header').append(t('RequestServerVersion', Version='Exchange2013_SP1'))
        ET.SubElement(envelope, f'{{{NS["s"]}}}Body').append(operation)
        data = ET.tostring(envelope, encoding='utf-8', xml_declaration=True)
        # Requests' stock HTTPAdapter does not retry POSTs. NTLM's 401 handshake
        # precedes execution; a lost application response must never replay a send.
        with self.lock:
            try:
                response = self.session.post(self.endpoint, data=data, timeout=(12, 30), verify=True,
                    allow_redirects=False, headers={'Content-Type': 'text/xml; charset=utf-8',
                                                   'SOAPAction': NS['m'] + '/' + name})
            except requests.RequestException:
                raise EwsError('TransportFailure') from None
        with response:
            self._local.date = response.headers.get('Date')
            status = response.status_code
            if status not in (200, 500):
                raise EwsError('HttpFailure', http_status=status)
            try:
                root = SafeXML.fromstring(response.content, forbid_dtd=True)
            except (ET.ParseError, DefusedXmlException):
                raise EwsError('InvalidXml', http_status=status) from None
            body = root.find('s:Body', NS)
            if root.tag != f'{{{NS["s"]}}}Envelope' or body is None or len(body) != 1:
                raise EwsError('InvalidEnvelope', http_status=status)
            if body[0].tag == f'{{{NS["s"]}}}Fault':
                fault = body[0]
                code = _code(fault.findtext('detail/e:ResponseCode', namespaces=NS))
                raise EwsError(code, http_status=status, retry_after=_delay(response, fault),
                               proven=code == 'ErrorServerBusy')
            wrapper = body[0]
            messages = wrapper.find('m:ResponseMessages', NS)
            if (wrapper.tag != f'{{{NS["m"]}}}{name}Response' or messages is None or len(messages) != 1
                    or messages[0].tag != f'{{{NS["m"]}}}{name}ResponseMessage'):
                raise EwsError('UnexpectedResponse', http_status=status)
            message = messages[0]
            codes = message.findall('m:ResponseCode', NS)
            if len(codes) != 1:
                raise EwsError('UnexpectedResponse', http_status=status)
            if status == 200 and message.get('ResponseClass') == 'Success' and codes[0].text == 'NoError':
                return message
            if message.get('ResponseClass') == 'Error':
                code = _code(codes[0].text)
                raise EwsError(code, http_status=status, retry_after=_delay(response, message),
                               proven=code != 'UnknownResponse')
            raise EwsError('UncertainResponse', http_status=status)

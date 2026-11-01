"""Small GitHub API client for the trusted maintenance workflows."""
from __future__ import annotations

import base64
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'GitHub API request failed (HTTP {status})')


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and (urlsplit(newurl).scheme != 'https'
                                       or urlsplit(newurl).netloc != urlsplit(req.full_url).netloc):
            redirected.remove_header('Authorization')
        return redirected


class GitHub:
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ['GITHUB_TOKEN']
        self.opener = build_opener(SafeRedirect())

    def request(self, method, path, body=None, *, raw=False):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('GitHub API paths must be relative to api.github.com')
        request = Request('https://api.github.com' + path, method=method,
                          data=None if body is None else json.dumps(body).encode('utf-8'), headers={
                              'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
                              'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json',
                          })
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read()
        except HTTPError as exc:
            raise APIError(exc.code) from None
        except URLError:
            raise RuntimeError('GitHub API transport failed') from None
        return data if raw else json.loads(data) if data else None

    def graphql(self, query, variables):
        result = self.request('POST', '/graphql', {'query': query, 'variables': variables})
        if result.get('errors') or not isinstance(result.get('data'), dict):
            # Error types (RATE_LIMITED, FORBIDDEN, ...) say what went wrong and carry no credential.
            types = sorted({str(error.get('type') or 'unknown') for error in result.get('errors') or []
                            if isinstance(error, dict)})
            raise RuntimeError(f"GitHub GraphQL request failed ({', '.join(types) or 'no data'})")
        return result['data']

    def pages(self, path, key=None):
        page = 1
        while True:
            result = self.request('GET', f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}')
            rows = result[key] if key else result
            yield from rows
            if len(rows) < 100:
                return
            page += 1

    def contents(self, repository, path, ref):
        row = self.request('GET', f'/repos/{repository}/contents/{quote(path, safe="/")}?ref={quote(ref, safe="")}')
        if row.get('type') != 'file' or row.get('encoding') != 'base64':
            raise ValueError('expected a regular JSON repository file')
        return json.loads(base64.b64decode(row['content']).decode('utf-8'))

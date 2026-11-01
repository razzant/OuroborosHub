"""Per-source request builders and normalizers for science_search.

Every function takes the shared ``Http`` and returns normalized records:
``{source, id, title, authors, year, venue, doi, url, abstract, citation_count,
open_access_url}``. Only public HTTP contracts of each provider are used. Each
adapter validates the provider's envelope before anything is cached: an HTTP
200 carrying an error object or a missing container is ``bad_response``, and
``not_found`` is raised only for a structurally valid, positive absence.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

from .transport import Http, SourceError

OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org/works"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ARXIV = "https://export.arxiv.org/api/query"
CORE = "https://api.core.ac.uk/v3/search/works/"  # the trailing slash is part of the public endpoint
OPENCITATIONS = "https://api.opencitations.net/index/v2"

INTERVAL = {"openalex": 0.1, "crossref": 0.1, "semantic_scholar": 1.0, "arxiv": 3.0, "core": 0.2,
            "opencitations": 0.2, "ncbi": 0.34, "ncbi_keyed": 0.1}
TTL_SEARCH, TTL_LOOKUP, TTL_CITATIONS = 3600.0, 86400.0, 86400.0
ARXIV_TIMEOUT_SEC = 60.0  # the arXiv export API routinely takes tens of seconds for a multi-term search
TITLE_CHARS, ABSTRACT_CHARS, MAX_AUTHORS = 500, 1200, 20
ID_CHARS, URL_CHARS, DOI_CHARS = 200, 1000, 200
OPENALEX_PAGE_MAX = 200
S2_FIELDS = "title,authors,year,venue,externalIds,abstract,citationCount,url,openAccessPdf"
OPENALEX_FIELDS = "id,doi,title,display_name,publication_year,authorships,primary_location,cited_by_count,abstract_inverted_index,open_access,referenced_works"
# Crossref rejects a select list with a field it cannot select (HTTP 400 validation-failure); every name here is
# from the documented selectable set. ``institution`` is not selectable, so the preprint venue falls back to publisher.
CROSSREF_SELECT = ["DOI", "title", "author", "issued", "container-title", "group-title", "publisher", "URL", "abstract",
                   "is-referenced-by-count"]
CROSSREF_SELECTABLE = frozenset(
    "DOI ISBN ISSN URL abstract accepted alternative-id approved archive article-number assertion author chair "
    "clinical-trial-number container-title content-created content-domain created degree deposited editor event funder "
    "group-title indexed is-referenced-by-count issn-type issue issued license link member original-title page posted "
    "prefix published published-online published-print publisher publisher-location reference references-count relation "
    "score short-container-title short-title standards-body subject subtitle title translator type update-policy "
    "update-to updated-by volume".split())
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")
_DOI_LINK = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)


# --------------------------------------------------------------------------- normalization

def clean_doi(value: Any) -> Optional[str]:
    """A DOI in lower case from a bare DOI, a ``doi:`` form or a doi.org link; None when it is not one.

    A doi.org link is a URL, so its percent escapes are decoded exactly once (a link from ``doi_url`` gives back
    its DOI); a bare or ``doi:`` DOI is taken literally, so a ``%`` in it stays part of the DOI.
    """
    text = str(value or "").strip()
    link = _DOI_LINK.match(text)
    if link:
        try:
            text = unquote(text[link.end():], errors="strict")
        except UnicodeDecodeError:  # escapes that are not UTF-8 name no DOI
            return None
    else:
        text = re.sub(r"^doi:", "", text, flags=re.IGNORECASE)
    text = text.strip().lower()
    if any(part in {".", ".."} for part in text.split("/")):
        return None  # HTTP clients normalize these segments instead of looking up the supplied identifier.
    return text if re.fullmatch(r"10\.\d{4,9}/\S+", text) and len(text) <= DOI_CHARS else None


def doi_path(doi: str) -> str:
    """A DOI as a URL path component: reserved characters such as ? # ; < > are percent-encoded, '/' is kept."""
    return quote(doi, safe="/")


def doi_url(doi: str) -> str:
    return f"https://doi.org/{doi_path(doi)}"


def ident_path(ident: str) -> str:
    """A provider identifier such as ``doi:10.1/x?y`` or ``W123`` as a path component: the prefix stays literal."""
    prefix, separator, rest = ident.partition(":")
    return f"{prefix}:{doi_path(rest)}" if separator else quote(ident, safe="")


def _trim(value: Any, limit: int) -> Optional[str]:
    text = _SPACES.sub(" ", _TAGS.sub(" ", str(value or ""))).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _scalar(value: Any, limit: int) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    return text if len(text) <= limit else text[:limit]


def _year(value: Any) -> Optional[int]:
    match = re.search(r"\d{4}", str(value or ""))
    return int(match.group()) if match else None


def _count(value: Any) -> Optional[int]:
    """A provider count as an int, or None when the field is missing or not a number (never a silent zero)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def record(source: str, ident: Any, title: Any, authors: List[Any], year: Any, venue: Any, doi: Any, url: Any,
           abstract: Any = None, citation_count: Any = None, open_access_url: Any = None) -> Dict[str, Any]:
    names = [_trim(a, 120) for a in authors if _trim(a, 120)]
    out = {"source": source, "id": _scalar(ident, ID_CHARS), "title": _trim(title, TITLE_CHARS),
           "authors": names[:MAX_AUTHORS], "year": year if isinstance(year, int) else _year(year),
           "venue": _trim(venue, 200), "doi": clean_doi(doi), "url": _scalar(url, URL_CHARS),
           "abstract": _trim(abstract, ABSTRACT_CHARS)}
    if len(names) > MAX_AUTHORS:
        out["authors_total"] = len(names)
    count = _count(citation_count)
    if count is not None:
        out["citation_count"] = count
    if open_access_url:
        out["open_access_url"] = _scalar(open_access_url, URL_CHARS)
    return out


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _error_text(data: Any) -> str:
    """The provider's own error wording from a JSON envelope, for the status message."""
    body = _dict(data)
    for key in ("error", "message", "ERROR"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
        if isinstance(value, (list, dict)) and value:
            return str(value)[:200]
    return ""


def _require(condition: bool, source: str, what: str, data: Any) -> None:
    if not condition:
        detail = _error_text(data)
        raise SourceError("bad_response", f"{source} returned no {what}" + (f": {detail}" if detail else ""))


# --------------------------------------------------------------------------- OpenAlex

def _openalex_params(keys: Dict[str, str], **params: Any) -> Dict[str, Any]:
    if keys.get("SCIENCE_SEARCH_MAILTO"):
        params["mailto"] = keys["SCIENCE_SEARCH_MAILTO"]
    if keys.get("OPENALEX_API_KEY"):
        params["api_key"] = keys["OPENALEX_API_KEY"]
    return params


def _openalex_list(data: Any) -> None:
    _require(isinstance(data, dict) and "error" not in data and isinstance(data.get("results"), list),
             "OpenAlex", "results list", data)


def _openalex_object(data: Any) -> None:
    _require(isinstance(data, dict) and "error" not in data and isinstance(data.get("id"), str) and bool(data.get("id")),
             "OpenAlex", "work object", data)


def _openalex_abstract(index: Any) -> Optional[str]:
    if not isinstance(index, dict) or not index:
        return None
    positions: Dict[int, str] = {}
    for word, places in index.items():
        for place in _list(places):
            if isinstance(place, int):
                positions[place] = str(word)
    return " ".join(positions[k] for k in sorted(positions))


def openalex_record(item: Dict[str, Any]) -> Dict[str, Any]:
    location = _dict(item.get("primary_location"))
    authors = [_dict(a.get("author")).get("display_name") for a in _list(item.get("authorships")) if isinstance(a, dict)]
    return record("openalex", item.get("id"), item.get("title") or item.get("display_name"),
                  [a for a in authors if a], item.get("publication_year"), _dict(location.get("source")).get("display_name"),
                  item.get("doi"), location.get("landing_page_url") or item.get("id"),
                  _openalex_abstract(item.get("abstract_inverted_index")), item.get("cited_by_count"),
                  _dict(item.get("open_access")).get("oa_url"))


def _openalex_page(http: Http, keys: Dict[str, str], query: str, filters: List[str], page: int,
                   per_page: int, ttl: float) -> List[Dict[str, Any]]:
    params = _openalex_params(keys, search=query, page=page, select=OPENALEX_FIELDS)
    params["per-page"] = per_page
    if filters:
        params["filter"] = ",".join(filters)
    data, _ = http.get("openalex", f"{OPENALEX}/works", params=params, interval=INTERVAL["openalex"], ttl=ttl,
                       validate=_openalex_list)
    return [item for item in data["results"] if isinstance(item, dict)]


def openalex_search(http: Http, query: str, year_from: Optional[int], year_to: Optional[int], limit: int,
                    offset: int, keys: Dict[str, str], *, open_access: bool = False) -> List[Dict[str, Any]]:
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if open_access:
        filters.append("is_oa:true")
    # OpenAlex pages instead of offsetting. While the window fits in one page from the start, one page covers it;
    # beyond that, pages of ``limit`` records are used and a window that straddles two pages reads both.
    if offset + limit <= OPENALEX_PAGE_MAX:
        items = _openalex_page(http, keys, query, filters, 1, offset + limit, TTL_SEARCH)[offset: offset + limit]
    else:
        page, skip = offset // limit + 1, offset % limit
        first = _openalex_page(http, keys, query, filters, page, limit, TTL_SEARCH)
        items = first[skip:]
        if skip and len(first) == limit:
            items += _openalex_page(http, keys, query, filters, page + 1, limit, TTL_SEARCH)[:skip]
        items = items[:limit]
    return [openalex_record(item) for item in items]


def openalex_work(http: Http, ident: str, keys: Dict[str, str]) -> Dict[str, Any]:
    """Raw OpenAlex work by W-id, doi:, pmid: or pmcid: identifier."""
    data, _ = http.get("openalex", f"{OPENALEX}/works/{ident_path(ident)}",
                       params=_openalex_params(keys, select=OPENALEX_FIELDS), interval=INTERVAL["openalex"],
                       ttl=TTL_LOOKUP, validate=_openalex_object)
    return data


def openalex_filter(http: Http, filter_value: str, limit: int, keys: Dict[str, str]) -> tuple[List[Dict[str, Any]], Any]:
    params = _openalex_params(keys, filter=filter_value, select=OPENALEX_FIELDS)
    params["per-page"] = limit
    data, _ = http.get("openalex", f"{OPENALEX}/works", params=params, interval=INTERVAL["openalex"],
                       ttl=TTL_CITATIONS, validate=_openalex_list)
    items = [openalex_record(i) for i in data["results"] if isinstance(i, dict)]
    return items, _count(_dict(data.get("meta")).get("count"))


# --------------------------------------------------------------------------- Crossref

def _crossref_params(keys: Dict[str, str], **params: Any) -> Dict[str, Any]:
    if keys.get("SCIENCE_SEARCH_MAILTO"):
        params["mailto"] = keys["SCIENCE_SEARCH_MAILTO"]
    return params


def _crossref_list(data: Any) -> None:
    body = _dict(data)
    _require(body.get("status") == "ok" and isinstance(_dict(body.get("message")).get("items"), list),
             "Crossref", "work list", data)


def _crossref_object(data: Any) -> None:
    body = _dict(data)
    _require(body.get("status") == "ok" and bool(_dict(body.get("message")).get("DOI")), "Crossref", "work object", data)


def crossref_record(item: Dict[str, Any], source: str = "crossref") -> Dict[str, Any]:
    authors = []
    for author in _list(item.get("author")):
        if isinstance(author, dict):
            authors.append(author.get("name") or " ".join(p for p in (author.get("given"), author.get("family")) if p))
    parts = _list(_dict(item.get("issued")).get("date-parts"))
    year = parts[0][0] if parts and _list(parts[0]) else None
    venue = (_list(item.get("container-title")) or _list(item.get("institution")) or [None])[0]
    if isinstance(venue, dict):
        venue = venue.get("name")
    venue = venue or item.get("publisher")
    doi = clean_doi(item.get("DOI"))
    return record(source, f"doi:{doi}" if doi else None, (_list(item.get("title")) or [None])[0], authors, year, venue,
                  doi, item.get("URL") or (doi_url(doi) if doi else None), item.get("abstract"),
                  item.get("is-referenced-by-count"))


def crossref_search(http: Http, query: str, year_from: Optional[int], year_to: Optional[int], limit: int,
                    offset: int, keys: Dict[str, str], *, preprints: bool = False) -> List[Dict[str, Any]]:
    filters = []
    if year_from:
        filters.append(f"from-pub-date:{year_from}")
    if year_to:
        filters.append(f"until-pub-date:{year_to}")
    if preprints:
        # bioRxiv and medRxiv deposit posted-content through Cold Spring Harbor Laboratory (member 246) under the
        # DOI prefix 10.1101. Crossref ANDs different filters, so the server returns exactly those records and
        # rows/offset count them; a client-side DOI filter would shorten pages and shift offsets.
        filters += ["type:posted-content", "member:246", "prefix:10.1101"]
    params = _crossref_params(keys, query=query, rows=limit, offset=offset, select=",".join(CROSSREF_SELECT))
    if filters:
        params["filter"] = ",".join(filters)
    data, _ = http.get("crossref", CROSSREF, params=params, interval=INTERVAL["crossref"], ttl=TTL_SEARCH,
                       validate=_crossref_list)
    items = [i for i in data["message"]["items"] if isinstance(i, dict)]
    return [crossref_record(i, "biorxiv" if preprints else "crossref") for i in items]


def crossref_work(http: Http, doi: str, keys: Dict[str, str]) -> Dict[str, Any]:
    data, _ = http.get("crossref", f"{CROSSREF}/{doi_path(doi)}", params=_crossref_params(keys),
                       interval=INTERVAL["crossref"], ttl=TTL_LOOKUP, validate=_crossref_object)
    return crossref_record(data["message"])


# --------------------------------------------------------------------------- Semantic Scholar

def _s2_headers(keys: Dict[str, str]) -> Dict[str, str]:
    return {"x-api-key": keys["SEMANTIC_SCHOLAR_API_KEY"]} if keys.get("SEMANTIC_SCHOLAR_API_KEY") else {}


def _s2_list(data: Any) -> None:
    _require(isinstance(data, dict) and "error" not in data and isinstance(data.get("data"), list),
             "Semantic Scholar", "paper list", data)


def _s2_object(data: Any) -> None:
    _require(isinstance(data, dict) and "error" not in data and bool(data.get("paperId")),
             "Semantic Scholar", "paper object", data)


def s2_record(item: Dict[str, Any]) -> Dict[str, Any]:
    ids = _dict(item.get("externalIds"))
    return record("semantic_scholar", item.get("paperId"), item.get("title"),
                  [_dict(a).get("name") for a in _list(item.get("authors"))], item.get("year"), item.get("venue"),
                  ids.get("DOI"), item.get("url"), item.get("abstract"), item.get("citationCount"),
                  _dict(item.get("openAccessPdf")).get("url"))


def s2_search(http: Http, query: str, year_from: Optional[int], year_to: Optional[int], limit: int, offset: int,
              keys: Dict[str, str]) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"query": query, "limit": limit, "offset": offset, "fields": S2_FIELDS}
    if year_from or year_to:
        params["year"] = f"{year_from or ''}-{year_to or ''}"
    data, _ = http.get("semantic_scholar", f"{SEMANTIC_SCHOLAR}/search", params=params, headers=_s2_headers(keys),
                       interval=INTERVAL["semantic_scholar"], ttl=TTL_SEARCH, cache_key_extra=str(bool(_s2_headers(keys))),
                       validate=_s2_list)
    return [s2_record(i) for i in data["data"] if isinstance(i, dict)]


def s2_work(http: Http, ident: str, keys: Dict[str, str]) -> Dict[str, Any]:
    data, _ = http.get("semantic_scholar", f"{SEMANTIC_SCHOLAR}/{ident_path(ident)}", params={"fields": S2_FIELDS},
                       headers=_s2_headers(keys), interval=INTERVAL["semantic_scholar"], ttl=TTL_LOOKUP,
                       validate=_s2_object)
    return s2_record(data)


# --------------------------------------------------------------------------- NCBI (PubMed / PMC)

def _ncbi_params(keys: Dict[str, str], **params: Any) -> tuple[Dict[str, Any], float]:
    params["tool"] = "ouroboros_science_search"
    if keys.get("SCIENCE_SEARCH_MAILTO"):
        params["email"] = keys["SCIENCE_SEARCH_MAILTO"]
    if keys.get("NCBI_API_KEY"):
        params["api_key"] = keys["NCBI_API_KEY"]
        return params, INTERVAL["ncbi_keyed"]
    return params, INTERVAL["ncbi"]


def _ncbi_search(data: Any) -> None:
    body = _dict(data)
    result = _dict(body.get("esearchresult"))
    _require("error" not in body and "ERROR" not in result and isinstance(result.get("idlist"), list),
             "NCBI", "search result", data)


def _ncbi_summary(data: Any) -> None:
    body = _dict(data)
    _require("error" not in body and isinstance(_dict(body.get("result")).get("uids"), list), "NCBI", "summary result", data)


def ncbi_record(item: Dict[str, Any], db: str) -> Dict[str, Any]:
    uid = str(item.get("uid") or "")
    ids = {_dict(i).get("idtype"): _dict(i).get("value") for i in _list(item.get("articleids"))}
    doi = ids.get("doi")
    if not doi and str(item.get("elocationid", "")).lower().startswith("doi:"):
        doi = str(item["elocationid"])[4:].strip()
    url = f"https://pubmed.ncbi.nlm.nih.gov/{uid}/" if db == "pubmed" else f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{uid}/"
    return record("pubmed" if db == "pubmed" else "pmc", f"{'pmid' if db == 'pubmed' else 'pmcid'}:{uid}", item.get("title"),
                  [_dict(a).get("name") for a in _list(item.get("authors"))], item.get("pubdate"),
                  item.get("fulljournalname") or item.get("source"), doi, url)


def _ncbi_item(result: Any, uid: str) -> Dict[str, Any]:
    """The summary NCBI returned for ``uid``, or {} when it returned an item error or nothing for it."""
    item = _dict(_dict(result).get(uid))
    return item if item.get("uid") and not item.get("error") else {}


def _ncbi_summaries(http: Http, db: str, ids: List[str],
                    keys: Dict[str, str]) -> tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Summaries for ``ids`` plus the ids NCBI could not summarize, with its wording.

    An item-level error such as "cannot get document summary" says the summary could not be read, not that the
    record does not exist, so it is reported as a failure, never as absence. An answer missing any requested
    summary is used for this call but never cached: asking again after NCBI recovers reads it afresh.
    """
    params, interval = _ncbi_params(keys, db=db, id=",".join(ids), retmode="json")
    data, _ = http.get("ncbi", f"{EUTILS}/esummary.fcgi", params=params, interval=interval, ttl=TTL_LOOKUP,
                       validate=_ncbi_summary,
                       complete=lambda body: all(_ncbi_item(_dict(body).get("result"), uid) for uid in ids))
    result = data["result"]
    records, failed = [], []
    for uid in ids:
        item = _ncbi_item(result, uid)
        if item:
            records.append(ncbi_record(item, db))
        else:
            failed.append({"id": uid, "message": _scalar(_dict(result.get(uid)).get("error"), 200)
                           or "no summary in NCBI's answer"})
    return records, failed


def pubmed_search(http: Http, query: str, year_from: Optional[int], year_to: Optional[int], limit: int, offset: int,
                  keys: Dict[str, str]) -> tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """PubMed records for one page, and the PMIDs on that page whose summaries NCBI could not return."""
    params, interval = _ncbi_params(keys, db="pubmed", term=query, retmax=limit, retstart=offset, retmode="json",
                                    sort="relevance")
    if year_from or year_to:
        params.update(datetype="pdat", mindate=str(year_from or 1800), maxdate=str(year_to or 2100))
    data, _ = http.get("ncbi", f"{EUTILS}/esearch.fcgi", params=params, interval=interval, ttl=TTL_SEARCH,
                       validate=_ncbi_search)
    ids = [str(i) for i in data["esearchresult"]["idlist"]]
    if not ids:
        return [], []
    records, failed = _ncbi_summaries(http, "pubmed", ids, keys)
    if not records:  # every listed PMID failed: the source failed, it did not find nothing
        raise SourceError("bad_response", f"NCBI listed {len(ids)} PubMed records but returned none of their "
                          f"summaries: {failed[0]['message']}")
    return records, failed


def ncbi_work(http: Http, db: str, uid: str, keys: Dict[str, str]) -> Dict[str, Any]:
    items, failed = _ncbi_summaries(http, db, [uid], keys)
    if not items:  # retrieval failed for this uid; NCBI did not say that the record does not exist
        reason = failed[0]["message"] if failed else "no summary in NCBI's answer"
        raise SourceError("bad_response", f"NCBI {db} returned no summary for {uid}: {reason}")
    return items[0]


# --------------------------------------------------------------------------- arXiv

_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
# arXiv reports an error as a valid Atom feed whose single entry has an id under /api/errors (API manual, 3.4).
ARXIV_ERROR_ID = "arxiv.org/api/errors"
# A query word is sent bare only when it cannot be read as arXiv query syntax; anything else is sent quoted.
_ARXIV_PLAIN = re.compile(r"\w[\w.'-]*")
_ARXIV_RESERVED = {"AND", "OR", "NOT", "ANDNOT", "TO"}
_ARXIV_TOKENS = re.compile(r'"([^"]*)"|(\S+)')


def arxiv_records(text: str) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise SourceError("bad_response", "arXiv returned an unreadable Atom feed") from None
    if root.tag != f"{{{_ATOM['a']}}}feed":
        raise SourceError("bad_response", "arXiv returned something other than an Atom feed")
    out = []
    for entry in root.findall("a:entry", _ATOM):
        url = (entry.findtext("a:id", default="", namespaces=_ATOM) or "").strip()
        if ARXIV_ERROR_ID in url:  # an error report, never a paper, whatever the HTTP status was
            detail = _trim(entry.findtext("a:summary", default="", namespaces=_ATOM), 200)
            raise SourceError("bad_response", f"arXiv answered with an error: {detail or url}")
        ident = url.rsplit("/abs/", 1)[-1] if "/abs/" in url else url
        out.append(record("arxiv", f"arXiv:{ident}" if ident else None, entry.findtext("a:title", default="", namespaces=_ATOM),
                          [n.text for n in entry.findall("a:author/a:name", _ATOM)],
                          entry.findtext("a:published", default="", namespaces=_ATOM),
                          entry.findtext("arxiv:journal_ref", default="", namespaces=_ATOM) or "arXiv",
                          entry.findtext("arxiv:doi", default="", namespaces=_ATOM), url,
                          entry.findtext("a:summary", default="", namespaces=_ATOM)))
    return out


def _arxiv_feed(text: Any) -> None:
    arxiv_records(str(text))  # parse failures and non-feed documents raise bad_response before caching


def arxiv_expression(query: str) -> str:
    """The plain query as arXiv search terms that must all match: ``all:word``, ``all:"quoted phrase"``.

    Operators, field prefixes and other query syntax typed by the user are not interpreted: such words are sent
    as quoted terms, so ``OR`` or ``au:smith`` is searched as text. Only the characters a quoted term cannot hold
    (a stray ``"`` and ``\\``) are dropped; other punctuation is kept.
    """
    terms = []
    for phrase, word in _ARXIV_TOKENS.findall(query):
        text = _SPACES.sub(" ", (phrase or word).replace('"', " ").replace("\\", " ")).strip()
        if not text:
            continue
        bare = not phrase and _ARXIV_PLAIN.fullmatch(text) and text not in _ARXIV_RESERVED
        terms.append(f"all:{text}" if bare else f'all:"{text}"')
    return " AND ".join(terms)


def arxiv_search(http: Http, query: str, year_from: Optional[int], year_to: Optional[int], limit: int,
                 offset: int) -> List[Dict[str, Any]]:
    expression = arxiv_expression(query)
    if not expression:
        raise SourceError("bad_request", "the query has no words arXiv can search for")
    if year_from or year_to:
        expression += f" AND submittedDate:[{year_from or 1991}01010000 TO {year_to or 2100}12312359]"
    text, _ = http.get("arxiv", ARXIV, params={"search_query": expression, "start": offset, "max_results": limit},
                       interval=INTERVAL["arxiv"], ttl=TTL_SEARCH, parse="xml", validate=_arxiv_feed,
                       request_timeout=ARXIV_TIMEOUT_SEC)
    return arxiv_records(text)


def arxiv_work(http: Http, ident: str) -> Dict[str, Any]:
    text, _ = http.get("arxiv", ARXIV, params={"id_list": ident, "max_results": 1}, interval=INTERVAL["arxiv"],
                       ttl=TTL_LOOKUP, parse="xml", validate=_arxiv_feed, request_timeout=ARXIV_TIMEOUT_SEC)
    items = [i for i in arxiv_records(text) if i.get("title")]
    if not items:
        raise SourceError("not_found", f"arXiv has no entry {ident}", status=404)
    return items[0]


# --------------------------------------------------------------------------- CORE

def _core_list(data: Any) -> None:
    _require(isinstance(data, dict) and "error" not in data and isinstance(data.get("results"), list),
             "CORE", "results list", data)


def core_search(http: Http, query: str, year_from: Optional[int], year_to: Optional[int], limit: int, offset: int,
                keys: Dict[str, str]) -> List[Dict[str, Any]]:
    expression = query
    if year_from:
        expression += f" AND yearPublished>={year_from}"
    if year_to:
        expression += f" AND yearPublished<={year_to}"
    headers = {"Authorization": f"Bearer {keys['CORE_API_KEY']}"} if keys.get("CORE_API_KEY") else {}
    data, _ = http.get("core", CORE, params={"q": expression, "limit": limit, "offset": offset}, headers=headers,
                       interval=INTERVAL["core"], ttl=TTL_SEARCH, cache_key_extra=str(bool(headers)), validate=_core_list)
    out = []
    for item in data["results"]:
        if isinstance(item, dict):
            out.append(record("core", f"core:{item.get('id')}", item.get("title"),
                              [_dict(a).get("name") for a in _list(item.get("authors"))], item.get("yearPublished"),
                              item.get("publisher"), item.get("doi"),
                              item.get("downloadUrl") or f"https://core.ac.uk/works/{item.get('id')}",
                              item.get("abstract"), item.get("citationCount"), item.get("downloadUrl")))
    return out


# --------------------------------------------------------------------------- OpenCitations

def _oc_dois(items: Any, field: str) -> List[str]:
    """One DOI per listed work (its first); works known only by other identifiers have none."""
    out = []
    for item in _list(items):
        for token in str(_dict(item).get(field, "")).split():
            doi = clean_doi(token[4:]) if token.lower().startswith("doi:") else None
            if doi:
                out.append(doi)
                break
    return out


def _oc_count(data: Any) -> None:
    _require(isinstance(data, list) and len(data) > 0 and _count(_dict(data[0]).get("count")) is not None,
             "OpenCitations", "count", data)


def _oc_list(data: Any) -> None:
    _require(isinstance(data, list), "OpenCitations", "list", data)


def opencitations_count(http: Http, doi: str) -> int:
    data, _ = http.get("opencitations", f"{OPENCITATIONS}/citation-count/doi:{doi_path(doi)}",
                       interval=INTERVAL["opencitations"], ttl=TTL_CITATIONS, validate=_oc_count)
    count = _count(_dict(data[0]).get("count"))
    assert count is not None
    return count


def opencitations_list(http: Http, doi: str, mode: str) -> tuple[List[str], int]:
    """DOIs citing (mode=citing) or cited by (mode=cited) the given DOI, and how many links OpenCitations listed
    (works known only by other identifiers have no DOI and are counted but not listed)."""
    endpoint, field = ("citations", "citing") if mode == "citing" else ("references", "cited")
    data, _ = http.get("opencitations", f"{OPENCITATIONS}/{endpoint}/doi:{doi_path(doi)}",
                       interval=INTERVAL["opencitations"], ttl=TTL_CITATIONS, validate=_oc_list)
    return _oc_dois(data, field), len(data)

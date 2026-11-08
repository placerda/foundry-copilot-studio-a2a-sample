"""Extract explicit textual attribution, not evidence that documents were fetched."""

from enum import Enum
import logging
import re

from contoso.router.core import CatalogEntry, Citation
from .common import ProtocolError, RawReply
from .diagnostics import _provider_label


class AttributionReason(str, Enum):
    UNSPECIFIED = "unspecified"
    INVALID_INPUT = "invalid_input"
    IDENTITY_MISMATCH = "identity_mismatch"
    EMPTY_TEXT = "empty_text"
    SOURCE_UNKNOWN = "source_unknown"
    VERSION_MISMATCH = "version_mismatch"
    HEADING_SOURCE_AMBIGUOUS = "heading_source_missing_or_ambiguous"
    SECTION_MISSING = "section_missing"
    SECTION_UNSUPPORTED = "section_unsupported"
    SECTION_MISSING_OR_UNSUPPORTED = "section_missing_or_unsupported"
    SECTION_LIST_MALFORMED = "section_list_malformed"
    ATTRIBUTION_MISSING = "attribution_missing"


class AttributionError(ProtocolError):
    """Missing, ambiguous, or unsupported attribution; contains no response text."""

    def __init__(self, message: str, *, reason: AttributionReason = AttributionReason.UNSPECIFIED):
        if not isinstance(reason, AttributionReason):
            raise ValueError("Unsupported attribution rejection reason")
        super().__init__(message)
        self.reason = reason


_HEADER = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<source>[A-Za-z0-9][A-Za-z0-9_.-]*)\s*,\s*"
    r"(?:(?:\"[^\"\n]*\"|'[^'\n]*')\s*,?\s*)?"
    r"Version\s+(?P<version>[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)"
    r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])",
    re.IGNORECASE,
)
_SECTION_LABEL = re.compile(r"^\s*[,;:.()\-]*\s*sections?\b\s*:?\s*", re.IGNORECASE)
_PREFIX = re.compile(r"^\s*[,;:.()\-]*\s*")
_CONTINUATION = re.compile(r"\s*(?:[,;]|&|\band\b)\s*")
_BARE_BOUNDARY = re.compile(r"(?:$|\n|[,;:.()\[\]\-]|[ \t]+(?:$|[,;:.()\[\]\-]|&|\band\b))")
_QUOTED = re.compile(r"""(?:"([^"]+)"|'([^']+)')""")
_FOOTNOTE = re.compile(r"\[\d+\]")
_HEADING = re.compile(r"^\s*(?:#{1,6}\s+.+|\*\*.+\*\*|__.+__)\s*(?:\[\d+\])?\s*$")
_FROM = re.compile(r"""\(\s*from\s+(?:"([^"]+)"|'([^']+)')\s*\)""", re.IGNORECASE)


def _presentation(text: str) -> str:
    return text.replace("**", "").replace("__", "").translate(str.maketrans({
        "“": '"', "”": '"', "‘": "'", "’": "'",
    }))


def _quoted_section(value: str, supported: frozenset[str]) -> str:
    if value in supported:
        return value
    # Citation punctuation may appear inside typographic quotation marks.
    value = value.rstrip(",.;")
    if value not in supported:
        raise AttributionError("Citation section is not configured",
                               reason=AttributionReason.SECTION_UNSUPPORTED)
    return value


def _sections(clause: str, supported: frozenset[str]) -> tuple[str, ...]:
    label = _SECTION_LABEL.match(clause)
    cursor = 0
    if label:
        cursor = label.end()
    else:
        prefix = _PREFIX.match(clause)
        if prefix:
            cursor = prefix.end()
    if not label and not _QUOTED.match(clause, cursor) and not any(
        clause.startswith(section, cursor) for section in supported
    ):
        return ()
    found = []
    while cursor < len(clause):
        quoted = _QUOTED.match(clause, cursor)
        if quoted:
            value = quoted.group(1) or quoted.group(2)
            section = _quoted_section(value, supported)
            internal_separator = value.endswith((",", ";"))
            internal_stop = value.endswith(".")
            cursor = quoted.end()
        else:
            candidates = [
                section for section in supported
                if clause.startswith(section, cursor)
                and _BARE_BOUNDARY.match(clause, cursor + len(section))
            ]
            if not candidates:
                raise AttributionError("Citation section is missing or unsupported",
                                       reason=AttributionReason.SECTION_MISSING_OR_UNSUPPORTED)
            section = max(candidates, key=len)
            internal_separator = internal_stop = False
            cursor += len(section)
        if section not in found:
            found.append(section)
        continuation = _CONTINUATION.match(clause, cursor)
        if continuation:
            cursor = continuation.end()
            if cursor == len(clause):
                break
            continue
        tail = clause[cursor:]
        cursor += len(tail) - len(tail.lstrip())
        if cursor == len(clause):
            break
        if internal_separator:
            continue
        if internal_stop or clause[cursor] in ".)]" or _FOOTNOTE.match(clause, cursor):
            break
        if "\n" in tail[:len(tail) - len(tail.lstrip())]:
            continue
        raise AttributionError("Citation section list is malformed or unsupported",
                               reason=AttributionReason.SECTION_LIST_MALFORMED)
    if not found:
        raise AttributionError("Citation requires an explicit configured section",
                               reason=AttributionReason.SECTION_MISSING)
    return tuple(found)


def _heading_sections(text: str) -> tuple[str, ...]:
    sections = []
    for line in text.splitlines():
        if not _HEADING.fullmatch(line):
            continue
        normalized = _presentation(line)
        for match in _FROM.finditer(normalized):
            sections.append(match.group(1) or match.group(2))
    return tuple(sections)


def extract_citations(entry: CatalogEntry, response: RawReply) -> tuple[Citation, ...]:
    """Bind explicit inline section lists or ``heading (from "section")``.

    Headings require one unambiguous explicitly identified/versioned source.
    Inline lists stay within their citation paragraph and end at sentence or
    footnote boundaries; descriptive prose is not another section-list item.
    Markdown bold and curly quotes are presentation-only. Configured versions
    may be ``N`` or ``Version N``; no other version aliases are inferred.
    Lists accept quoted or exact unquoted names, separated by commas, semicolons,
    newlines, or "and", with an optional colon after the section label.
    Unknown explicit sections fail closed instead of being guessed.
    This verifies attribution syntax only, never factuality or document retrieval.
    """
    if not isinstance(entry, CatalogEntry) or not isinstance(response, RawReply):
        raise AttributionError("Configured entry and raw reply are required",
                               reason=AttributionReason.INVALID_INPUT)
    if (response.provider_id != entry.provider_id
            or response.capability != entry.capability
            or response.variant != entry.variant or response.protocol != entry.protocol
            or response.endpoint != entry.endpoint):
        raise AttributionError("Reply identity does not match the configured provider",
                               reason=AttributionReason.IDENTITY_MISMATCH)
    if not isinstance(response.text, str) or not response.text.strip():
        raise AttributionError("Reply contains no attribution text", reason=AttributionReason.EMPTY_TEXT)
    text = _presentation(response.text)
    sources = {source.source_id: source for source in entry.sources}
    citations = []
    headers = list(_HEADER.finditer(text))
    identified = {}
    for header in headers:
        source = sources.get(header["source"])
        if source is None:
            raise AttributionError("Citation source is not configured",
                                   reason=AttributionReason.SOURCE_UNKNOWN)
        expected_version = re.sub(
            r"^Version\s+", "", source.version, count=1, flags=re.IGNORECASE,
        )
        if header["version"] != expected_version:
            raise AttributionError("Citation version does not match configured source",
                                   reason=AttributionReason.VERSION_MISMATCH)
        identified[source.source_id] = source
    headings = _heading_sections(response.text)
    if headings:
        if len(identified) != 1:
            raise AttributionError("Section heading source attribution is missing or ambiguous",
                                   reason=AttributionReason.HEADING_SOURCE_AMBIGUOUS)
        source = next(iter(identified.values()))
        for heading in headings:
            section = _quoted_section(heading, source.sections)
            citation = Citation(source.source_id, source.provider_id, source.version, section)
            if citation not in citations:
                citations.append(citation)
    for index, header in enumerate(headers):
        source = identified[header["source"]]
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        paragraph_end = re.search(r"\n\s*\n", text[header.end():end])
        if paragraph_end:
            end = header.end() + paragraph_end.start()
        sections = _sections(text[header.end():end], source.sections)
        if not sections and not headings:
            raise AttributionError("Citation requires an explicit configured section",
                                   reason=AttributionReason.SECTION_MISSING)
        for section in sections:
            citation = Citation(source.source_id, source.provider_id, source.version, section)
            if citation not in citations:
                citations.append(citation)
    if not citations:
        raise AttributionError("Reply has no explicit configured attribution",
                               reason=AttributionReason.ATTRIBUTION_MISSING)
    return tuple(citations)


def log_attribution_rejection(
    logger: logging.Logger, entry: CatalogEntry, response: RawReply, error: AttributionError,
) -> None:
    """Log structure in the first 16384 characters, never citation values or reply text.

    Presence flags are lexical observations only; they cannot establish attribution.
    Counts saturate at ``many`` above 32. Parsing/grounding rules remain unchanged.
    """
    raw = response.text if isinstance(response, RawReply) and isinstance(response.text, str) else ""
    window = raw[:16384]
    text = _presentation(window)
    sources = {source.source_id: source for source in entry.sources}
    headers = list(_HEADER.finditer(text))
    known_headers = [header for header in headers if header["source"] in sources]
    versions = {
        source.source_id: re.sub(r"^Version\s+", "", source.version, count=1, flags=re.IGNORECASE)
        for source in entry.sources
    }
    version_matches = sum(
        header["version"] == versions[header["source"]] for header in known_headers
    )
    headings = _heading_sections(window)
    sections = frozenset(section for source in entry.sources for section in source.sections)
    known_headings = sum(heading.rstrip(",.;") in sections for heading in headings)
    id_seen = any(re.search(
        rf"(?<![A-Za-z0-9_.-]){re.escape(source_id)}(?![A-Za-z0-9_.-])", text,
    ) for source_id in sources)
    version_seen = any(re.search(
        rf"(?i:Version)\s+{re.escape(version)}(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])", text,
    ) for version in versions.values())
    section_seen = any(section in text for section in sections)

    def count(value: int) -> str:
        return str(value) if value <= 32 else "many"

    logger.warning(
        "contoso_attribution provider=%s status=rejected reason=%s headers=%s known_headers=%s "
        "version_matches=%s headings=%s known_headings=%s id_seen=%s version_seen=%s "
        "section_seen=%s truncated=%s",
        _provider_label(entry), error.reason.value, count(len(headers)), count(len(known_headers)),
        count(version_matches), count(len(headings)), count(known_headings),
        "yes" if id_seen else "no", "yes" if version_seen else "no",
        "yes" if section_seen else "no", "yes" if len(raw) > len(window) else "no",
    )

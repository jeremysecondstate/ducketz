from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import requests

from app.services.fmp_corporate_data import FmpCorporateDataProvider, SEC_FILINGS_LIMIT

SEC_USER_AGENT_ENV = "SEC_USER_AGENT"
SEC_TEXT_TIMEOUT_SECONDS = 45
SEC_FILINGS_LOOKBACK_DAYS = 370
EVIDENCE_SNIPPET_RADIUS = 260

CAPITAL_STRUCTURE_FORM_TYPES = {
    "S-1",
    "S-1/A",
    "S-3",
    "S-3/A",
    "F-1",
    "F-1/A",
    "F-3",
    "F-3/A",
    "424B",
    "424B1",
    "424B2",
    "424B3",
    "424B4",
    "424B5",
    "8-K",
    "8-K/A",
    "6-K",
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "20-F",
    "20-F/A",
    "DEF 14A",
    "SC 13D",
    "SC 13D/A",
    "SC 13G",
    "SC 13G/A",
}

INSTRUMENT_EVENT_PATTERNS = (
    (
        "Warrants",
        (
            r"\bwarrants?\s+(?:to\s+purchase|are\s+(?:currently\s+)?exercisable|remain\s+outstanding|were\s+exercised|expired|were\s+redeemed)\b",
            r"\b(?:exercise|strike)\s+price\s+(?:of|for)\s+(?:the\s+)?warrants?\b",
            r"\bwarrant\s+(?:shares?|holders?|agreement|certificate)\b",
        ),
    ),
    (
        "Convertible securities",
        (
            r"\bconvertible\s+(?:notes?|debt|debentures?|securities|senior\s+notes?|preferred\s+(?:stock|shares?))\b",
            r"\bconversion\s+(?:price|rate|ratio)\b",
        ),
    ),
    (
        "Preferred stock",
        (r"\b(?:series\s+[a-z0-9-]+\s+)?preferred\s+(?:stock|shares?)\b",),
    ),
    (
        "At-the-market offering",
        (
            r"\bat-the-market\s+(?:offering|program|facility)\b",
            r"\bat\s+the\s+market\s+(?:offering|program|facility)\b",
            r"\bequity\s+distribution\s+agreement\b",
        ),
    ),
    (
        "Securities offering",
        (
            r"\b(?:public|registered|direct)\s+offering\s+of\s+(?:up\s+to\s+)?(?:[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:thousand|million|billion)?\s+)?(?:shares?|units?|securities|common\s+stock)\b",
            r"\b(?:public\s+offering\s+price|offering\s+price|price\s+to\s+the\s+public|purchase\s+price\s+per\s+share)\b",
        ),
    ),
    (
        "Resale registration",
        (
            r"\bresale\s+(?:registration\s+statement|prospectus|of\s+(?:shares?|securities|common\s+stock))\b",
            r"\bselling\s+(?:stockholders?|shareholders?)\b",
        ),
    ),
    (
        "Shelf registration",
        (
            r"\bshelf\s+(?:registration|offering|prospectus)\b",
            r"\buniversal\s+shelf\b",
        ),
    ),
    (
        "Dilution disclosure",
        (
            r"\b(?:substantial|immediate)\s+dilution\b",
            r"\bdilutive\s+(?:effect|impact)\b",
            r"\bdilution\s+to\s+(?:new|existing|our)\s+(?:investors|stockholders|shareholders)\b",
        ),
    ),
)

COMPILED_INSTRUMENT_EVENT_PATTERNS = tuple(
    (label, tuple(re.compile(pattern, flags=re.IGNORECASE) for pattern in patterns))
    for label, patterns in INSTRUMENT_EVENT_PATTERNS
)

KEYWORD_ONLY_PATTERN = re.compile(
    r"\b(?:warrants?|warranties|convertible|preferred\s+(?:stock|shares?)|dilution|diluted\s+(?:earnings|shares?))\b",
    flags=re.IGNORECASE,
)


class SecCapitalStructureScanner:
    source = "sec"

    def __init__(
        self,
        *,
        fmp_provider: FmpCorporateDataProvider | None = None,
        user_agent: str | None = None,
        timeout_seconds: int = SEC_TEXT_TIMEOUT_SECONDS,
    ) -> None:
        self.fmp_provider = fmp_provider or FmpCorporateDataProvider()
        configured_user_agent = user_agent if user_agent is not None else os.getenv(SEC_USER_AGENT_ENV, "").strip()
        self.user_agent = configured_user_agent or "ducketz/0.1 local-research"
        self.timeout_seconds = timeout_seconds

    def fetch_capital_structure_rows(
        self,
        symbol: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        clean_symbol = _symbol(symbol)
        filings = self._fetch_fmp_filings(clean_symbol)
        selected_filings = [_filing for _filing in filings if _is_relevant_filing(_filing)]

        scan_rows: list[dict[str, Any]] = []
        raw_text_rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for index, filing in enumerate(selected_filings):
            document_url = _filing_document_url(filing)
            if not document_url:
                errors.append(_filing_error_row(clean_symbol, filing, "MissingDocumentUrl", "FMP filing row had no SEC document URL."))
                continue

            try:
                text = self._fetch_text(document_url)
                scan_rows.extend(_scan_filing_text(clean_symbol, filing, document_url, text, index))
                raw_text_rows.append(_raw_text_row(clean_symbol, filing, document_url, text, index))
            except Exception as exc:
                errors.append(_filing_error_row(clean_symbol, filing, type(exc).__name__, str(exc)))

        return scan_rows, raw_text_rows, errors

    def _fetch_fmp_filings(self, symbol: str) -> list[Mapping[str, Any]]:
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=SEC_FILINGS_LOOKBACK_DAYS)
        payload = self.fmp_provider._get_json(
            "sec-filings-search/symbol",
            {
                "symbol": symbol,
                "from": start.isoformat(),
                "to": today.isoformat(),
                "page": 0,
                "limit": SEC_FILINGS_LIMIT,
            },
        )

        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, Mapping)]
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, list):
                return [row for row in data if isinstance(row, Mapping)]
            return [payload]
        return []

    def _fetch_text(self, url: str) -> str:
        response = requests.get(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"SEC text request failed with HTTP {response.status_code}: {url}")
        return _clean_text(response.text)


def _scan_filing_text(
    symbol: str,
    filing: Mapping[str, Any],
    document_url: str,
    text: str,
    row_index: int,
) -> list[dict[str, Any]]:
    analyses = _instrument_event_analyses(text)
    filing_date = _text_value(filing, "filingDate", "date")
    common: dict[str, Any] = {
        "symbol": symbol,
        "source": "sec",
        "endpoint": "filing_text",
        "request_key": "capital_structure_terms",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "form_type": _text_value(filing, "type", "form", "formType"),
        "filing_date": filing_date,
        "accepted_date": _text_value(filing, "acceptedDate"),
        "accession_number": _accession_number(filing),
        "document_url": document_url,
    }
    return [
        {
            **common,
            "row_index": row_index * 100 + analysis_index,
            **analysis,
        }
        for analysis_index, analysis in enumerate(analyses)
    ]


def _instrument_event_analyses(text: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[object, ...]] = set()
    for instrument_event, patterns in COMPILED_INSTRUMENT_EVENT_PATTERNS:
        instrument_count = 0
        for pattern in patterns:
            for match in pattern.finditer(text):
                quote = _snippet_for_span(text, match.start(), match.end(), radius=360)
                analysis = _instrument_event_analysis(instrument_event, quote)
                key = (
                    analysis["instrument_name"],
                    analysis["evidence_state"],
                    analysis["exercise_price"],
                    analysis["conversion_price"],
                    analysis["offering_price"],
                    analysis["expiration_date"],
                    analysis["quantity"],
                )
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(analysis)
                instrument_count += 1
                if instrument_count >= 8:
                    break
            if instrument_count >= 8:
                break

    if candidates:
        return candidates

    keyword_match = KEYWORD_ONLY_PATTERN.search(text)
    if keyword_match is not None:
        quote = _snippet_for_span(text, keyword_match.start(), keyword_match.end(), radius=360)
        unrelated = _unrelated_keyword_context(quote)
        return [
            {
                "instrument_event": "Unclassified mention",
                "instrument_name": "Unclassified mention",
                "evidence_state": "unrelated" if unrelated else "insufficient_evidence",
                "evidence_quality": "Keyword only",
                "state_basis": (
                    "The excerpt uses accounting, product-warranty, or boilerplate language rather than describing a security."
                    if unrelated
                    else "The excerpt contains a capital-structure word but does not identify an instrument, event, or current status."
                ),
                "quantity": None,
                "quantity_unit": "",
                "exercise_price": None,
                "conversion_price": None,
                "offering_price": None,
                "offering_size": None,
                "expiration_date": "",
                "event_date": "",
                "dilution_percentage": None,
                "exercisability": "Not established",
                "restrictions": "Not established",
                "mechanism": "No issuance, conversion, exercise, or resale mechanism is supported by this excerpt.",
                "uncertainty": "A keyword match alone cannot establish a current capital-structure concern.",
                "is_current_concern": False,
                "evidence_quote": quote,
            }
        ]

    return [
        {
            "instrument_event": "No instrument or event identified",
            "instrument_name": "No instrument or event identified",
            "evidence_state": "insufficient_evidence",
            "evidence_quality": "No relevant evidence",
            "state_basis": "The filing scan did not identify filing text that supports a capital-structure instrument or event.",
            "quantity": None,
            "quantity_unit": "",
            "exercise_price": None,
            "conversion_price": None,
            "offering_price": None,
            "offering_size": None,
            "expiration_date": "",
            "event_date": "",
            "dilution_percentage": None,
            "exercisability": "Not established",
            "restrictions": "Not established",
            "mechanism": "No capital-structure mechanism is supported by the scanned filing text.",
            "uncertainty": "Absence of extracted evidence is not proof that no instrument exists.",
            "is_current_concern": False,
            "evidence_quote": "No relevant excerpt was identified.",
        }
    ]


def _instrument_event_analysis(instrument_event: str, quote: str) -> dict[str, Any]:
    state, state_basis = _evidence_state(quote, instrument_event)
    quantity, quantity_unit = _security_quantity(quote)
    exercise_price = _term_price(quote, "exercise")
    conversion_price = _term_price(quote, "conversion")
    offering_price = _term_price(quote, "offering")
    offering_size = _offering_size(quote)
    expiration_date = _expiration_date(quote)
    event_date = _event_date(quote)
    dilution_percentage = _dilution_percentage(quote)
    exercisability = _exercisability(quote)
    restrictions = _restrictions(quote)
    has_term = any(
        value not in (None, "", "Not established")
        for value in (
            quantity,
            exercise_price,
            conversion_price,
            offering_price,
            offering_size,
            expiration_date,
            event_date,
            dilution_percentage,
            exercisability,
            restrictions,
        )
    )
    supported_state = state not in {"insufficient_evidence", "unrelated"}
    if supported_state and has_term:
        evidence_quality = "Confirmed terms"
    elif supported_state or has_term:
        evidence_quality = "Partial terms"
    else:
        evidence_quality = "Keyword only"
    is_current_concern = (
        state in {"outstanding", "pending"}
        and evidence_quality != "Keyword only"
        and instrument_event not in {"Shelf registration", "Dilution disclosure"}
    )
    return {
        "instrument_event": instrument_event,
        "instrument_name": _instrument_name(instrument_event, quote),
        "evidence_state": state,
        "evidence_quality": evidence_quality,
        "state_basis": state_basis,
        "quantity": quantity,
        "quantity_unit": quantity_unit,
        "exercise_price": exercise_price,
        "conversion_price": conversion_price,
        "offering_price": offering_price,
        "offering_size": offering_size,
        "expiration_date": expiration_date,
        "event_date": event_date,
        "dilution_percentage": dilution_percentage,
        "exercisability": exercisability,
        "restrictions": restrictions,
        "mechanism": _instrument_mechanism(instrument_event, state),
        "uncertainty": _instrument_uncertainty(
            instrument_event,
            state,
            quantity,
            exercise_price,
            conversion_price,
            offering_price,
            expiration_date,
            event_date,
        ),
        "is_current_concern": is_current_concern,
        "evidence_quote": quote,
    }


def _instrument_name(instrument_event: str, quote: str) -> str:
    patterns = {
        "Warrants": r"\bSeries\s+[A-Z0-9-]+\s+(?:common\s+stock\s+)?warrants\b",
        "Convertible securities": r"\b(?:20\d{2}\s+)?convertible\s+(?:senior\s+)?(?:notes?|debentures?)\b",
        "Preferred stock": r"\bSeries\s+[A-Z0-9-]+\s+(?:convertible\s+)?preferred\s+(?:stock|shares?)\b",
    }
    pattern = patterns.get(instrument_event)
    if pattern is None:
        return instrument_event
    match = re.search(pattern, quote, flags=re.IGNORECASE)
    return " ".join(match.group(0).split()) if match is not None else instrument_event


def _evidence_state(quote: str, instrument_event: str) -> tuple[str, str]:
    lower = quote.lower()
    state_patterns = (
        ("unrelated", _unrelated_keyword_context(quote), "The excerpt is accounting, product-warranty, or boilerplate text rather than a security disclosure."),
        ("superseded", bool(re.search(r"\b(?:superseded|replaced by|terminated and replaced|no longer in effect)\b", lower)), "The excerpt says the prior instrument or arrangement was superseded or replaced."),
        ("expired", bool(re.search(r"\b(?:have|has|had|were|was)?\s*expired\b", lower)), "The excerpt says the instrument expired."),
        ("redeemed", bool(re.search(r"\b(?:have|has|had|were|was)?\s*redeemed\b", lower)), "The excerpt says the instrument was redeemed."),
        ("exercised", bool(re.search(r"\b(?:were|was|have been|has been)\s+(?:exercised|converted)\b", lower)), "The excerpt says the instrument was exercised or converted."),
        ("completed", bool(re.search(r"\b(?:offering|transaction)\s+(?:closed|was completed|has been completed)|\bwe\s+completed\s+(?:the|an|a)?\s*(?:public|registered|direct)?\s*offering\b", lower)), "The excerpt says the offering or transaction was completed."),
        ("outstanding", bool(re.search(r"\b(?:remain|remained|are|were)\s+outstanding\b|\bcurrently\s+exercisable\b|\bare\s+exercisable\b", lower)), "The excerpt says the instrument is outstanding or currently exercisable as of the filing disclosure."),
        ("pending", bool(re.search(r"\b(?:proposed offering|intends?\s+to\s+offer|expected\s+to\s+close|subject\s+to\s+(?:closing|completion)|has not yet closed)\b", lower)), "The excerpt describes a proposed or not-yet-completed transaction."),
        ("authorized", bool(re.search(r"\b(?:authorized|approved)(?:\s+the\s+company)?\s+(?:to\s+issue|for\s+issuance)|\bmay\s+(?:offer|issue|sell)\b", lower)), "The excerpt describes authority or capacity to issue securities, not a completed issuance."),
        ("historical", bool(re.search(r"\b(?:previously|historically|during the year ended|we issued|we sold|were issued)\b", lower)), "The excerpt describes a past issuance or prior-period disclosure without establishing that the instrument remains outstanding."),
    )
    for state, matched, basis in state_patterns:
        if matched:
            return state, basis
    return (
        "insufficient_evidence",
        f"The excerpt identifies {instrument_event.lower()} but does not reliably establish whether it is authorized, pending, completed, outstanding, expired, redeemed, exercised, or superseded.",
    )


def _unrelated_keyword_context(quote: str) -> bool:
    lower = quote.lower()
    unrelated_patterns = (
        r"\brepresentations?\s+and\s+warrant(?:y|ies)\b",
        r"\bproduct\s+warrant(?:y|ies)\b",
        r"\bwarranty\s+(?:expense|reserve|claims?)\b",
        r"\bweighted[- ]average\s+diluted\s+shares\b",
        r"\bdiluted\s+(?:earnings|net income)\s+per\s+share\b",
    )
    return any(re.search(pattern, lower) is not None for pattern in unrelated_patterns)


def _security_quantity(quote: str) -> tuple[float | None, str]:
    match = re.search(
        r"\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*(thousand|million|billion)?\s+(shares?|units?|warrants?)\b",
        quote,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None, ""
    multiplier = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}.get(
        (match.group(2) or "").lower(),
        1,
    )
    return float(match.group(1).replace(",", "")) * multiplier, match.group(3).lower()


def _term_price(quote: str, term: str) -> float | None:
    labels = {
        "exercise": r"(?:exercise|strike)\s+price",
        "conversion": r"conversion\s+price",
        "offering": r"(?:public\s+offering\s+price|offering\s+price|price\s+to\s+the\s+public|purchase\s+price\s+per\s+share)",
    }
    match = re.search(
        rf"\b{labels[term]}\s+(?:of|for|is|was|equal\s+to|at)?\s*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        quote,
        flags=re.IGNORECASE,
    )
    return float(match.group(1).replace(",", "")) if match is not None else None


def _offering_size(quote: str) -> float | None:
    match = re.search(
        r"\b(?:aggregate\s+offering|offering\s+size|gross\s+proceeds|up\s+to)\s+(?:of|were|was|approximately)?\s*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(thousand|million|billion)?\b",
        quote,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    multiplier = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}.get(
        (match.group(2) or "").lower(),
        1,
    )
    return float(match.group(1).replace(",", "")) * multiplier


def _expiration_date(quote: str) -> str:
    match = re.search(
        r"\b(?:expire|expires|expired|expiration\s+date\s+(?:is|of))\s+(?:on\s+)?((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
        quote,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match is not None else ""


def _event_date(quote: str) -> str:
    date_pattern = r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})"
    for pattern in (
        rf"\b(?:as\s+of|on)\s+{date_pattern}",
        rf"\b(?:expected\s+to\s+close|closed|completed|expired|redeemed|exercised|converted)\s+on\s+{date_pattern}",
    ):
        match = re.search(pattern, quote, flags=re.IGNORECASE)
        if match is not None:
            return match.group(1)
    return ""


def _dilution_percentage(quote: str) -> float | None:
    match = re.search(
        r"\b(?:dilution\s+(?:of|equal\s+to)|represent(?:s|ing)?|equal\s+to)\s+([0-9]+(?:\.[0-9]+)?)\s*%",
        quote,
        flags=re.IGNORECASE,
    )
    return float(match.group(1)) if match is not None else None


def _exercisability(quote: str) -> str:
    lower = quote.lower()
    if re.search(r"\b(?:not yet|not currently)\s+exercisable\b", lower):
        return "Not yet exercisable"
    if re.search(r"\bcurrently\s+exercisable\b|\bare\s+exercisable\b", lower):
        return "Currently exercisable"
    if re.search(r"\bexercisable\s+(?:beginning|after|on)\b", lower):
        return "Future exercisability described"
    return "Not established"


def _restrictions(quote: str) -> str:
    match = re.search(
        r"\b(lock-up\s+period\s+of\s+[^.;]+|restricted\s+period\s+ending\s+[^.;]+|may\s+not\s+(?:offer|sell|transfer)\s+[^.;]+)",
        quote,
        flags=re.IGNORECASE,
    )
    return " ".join(match.group(1).split()) if match is not None else "Not established"


def _instrument_mechanism(instrument_event: str, state: str) -> str:
    mechanisms = {
        "Warrants": "Exercise can issue shares under the warrant terms; whether holders exercise depends on exercisability and economics.",
        "Convertible securities": "Conversion can replace the security with shares under the conversion terms.",
        "Preferred stock": "Preferred stock affects senior claims and may affect common-share count if conversion rights exist.",
        "At-the-market offering": "Company sales into the market can add shares over time, subject to the program terms and actual sales.",
        "Securities offering": "Issuance can add shares or units and a completed sale can add holder supply.",
        "Resale registration": "Registration can permit named holders to resell; it does not prove that they sold.",
        "Shelf registration": "Registration creates financing capacity; it does not prove that securities were issued.",
        "Dilution disclosure": "The filing describes a possible ownership-per-share effect but does not itself identify an issuance.",
    }
    mechanism = mechanisms[instrument_event]
    if state in {"expired", "redeemed", "superseded"}:
        return f"{mechanism} The cited state indicates this mechanism is no longer active for the described instrument."
    if state in {"completed", "exercised", "historical"}:
        return f"{mechanism} The cited state describes a past event, not an uncompleted current event."
    return mechanism


def _instrument_uncertainty(
    instrument_event: str,
    state: str,
    quantity: float | None,
    exercise_price: float | None,
    conversion_price: float | None,
    offering_price: float | None,
    expiration_date: str,
    event_date: str,
) -> str:
    missing: list[str] = []
    if instrument_event in {"Warrants", "Convertible securities", "Preferred stock", "Securities offering", "Resale registration"} and quantity is None:
        missing.append("quantity")
    if instrument_event == "Warrants" and exercise_price is None:
        missing.append("exercise price")
    if instrument_event in {"Convertible securities", "Preferred stock"} and conversion_price is None:
        missing.append("conversion price")
    if instrument_event == "Securities offering" and offering_price is None:
        missing.append("offering price")
    if instrument_event == "Warrants" and not expiration_date:
        missing.append("expiration date")
    if state in {"pending", "completed", "outstanding", "expired", "redeemed", "exercised", "superseded"} and not event_date:
        missing.append("event date")
    if state == "insufficient_evidence":
        missing.append("current instrument state")
    if not missing:
        return "The cited excerpt supplies the displayed terms; later filings may change the instrument state."
    return f"The cited excerpt does not reliably establish {', '.join(missing)}; no missing value was inferred."


def _raw_text_row(
    symbol: str,
    filing: Mapping[str, Any],
    document_url: str,
    text: str,
    row_index: int,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "source": "sec",
        "endpoint": "filing_text",
        "request_key": "capital_structure_filing_text",
        "row_index": row_index,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "form_type": _text_value(filing, "type", "form", "formType"),
        "filing_date": _text_value(filing, "filingDate", "date"),
        "accepted_date": _text_value(filing, "acceptedDate"),
        "cik": _filing_cik(filing),
        "accession_number": _accession_number(filing),
        "document_url": document_url,
        "text_length": len(text),
        "document_text": text,
    }


def _filing_error_row(symbol: str, filing: Mapping[str, Any], error_type: str, error_message: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "source": "sec",
        "category": "capital_structure",
        "request_key": "capital_structure_terms",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "form_type": _text_value(filing, "type", "form", "formType"),
        "filing_date": _text_value(filing, "filingDate", "date"),
        "cik": _filing_cik(filing),
        "accession_number": _accession_number(filing),
        "document_url": _filing_document_url(filing) or "",
        "error_type": error_type,
        "error_message": error_message,
    }


def _filing_document_url(filing: Mapping[str, Any]) -> str:
    url = _text_value(filing, "finalLink", "final_link", "link", "url", "documentUrl", "document_url")
    if not url:
        return ""
    return _repair_sec_archive_url(url, filing)


def _repair_sec_archive_url(url: str, filing: Mapping[str, Any]) -> str:
    if "/Archives/edgar/data//" not in url:
        return url

    cik = _filing_cik(filing)
    if not cik:
        accession_digits = _accession_digits(filing)
        if len(accession_digits) >= 10:
            cik = str(int(accession_digits[:10]))

    if not cik:
        return url

    return url.replace("/Archives/edgar/data//", f"/Archives/edgar/data/{cik}/", 1)


def _filing_cik(filing: Mapping[str, Any]) -> str:
    value = _text_value(filing, "cik", "cikNumber", "companyCik")
    digits = re.sub(r"\D", "", value)
    if digits:
        return str(int(digits))

    accession_digits = _accession_digits(filing)
    if len(accession_digits) >= 10:
        return str(int(accession_digits[:10]))

    return ""


def _accession_number(filing: Mapping[str, Any]) -> str:
    value = _text_value(filing, "accessionNumber", "accessionNo", "accession")
    if value:
        return value

    url = _text_value(filing, "finalLink", "final_link", "link", "url", "documentUrl", "document_url")
    match = re.search(r"/Archives/edgar/data/+\d*/(\d{18})/", url)
    if match is not None:
        digits = match.group(1)
        return f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"

    return ""


def _accession_digits(filing: Mapping[str, Any]) -> str:
    accession = _accession_number(filing)
    if accession:
        return re.sub(r"\D", "", accession)

    url = _text_value(filing, "finalLink", "final_link", "link", "url", "documentUrl", "document_url")
    match = re.search(r"/Archives/edgar/data/+\d*/(\d{18})/", url)
    return match.group(1) if match is not None else ""


def _is_relevant_filing(filing: Mapping[str, Any]) -> bool:
    form_type = _text_value(filing, "type", "form", "formType").upper()
    if form_type in CAPITAL_STRUCTURE_FORM_TYPES:
        return True
    return any(form_type.startswith(prefix) for prefix in ("424B", "SC 13D", "SC 13G"))


def _text_value(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _snippet_for_span(text: str, start: int, end: int, radius: int = EVIDENCE_SNIPPET_RADIUS) -> str:
    floor = max(0, start - radius)
    ceiling = min(len(text), end + radius)
    prior_period = text.rfind(". ", floor, start)
    prior_break = text.rfind("\n", floor, start)
    starts = [floor]
    if prior_period >= 0:
        starts.append(prior_period + 2)
    if prior_break >= 0:
        starts.append(prior_break + 1)
    snippet_start = max(starts)
    next_period = text.find(". ", end, ceiling)
    next_break = text.find("\n", end, ceiling)
    endings = [value for value in (next_period + 1, next_break) if value > end]
    snippet_end = min(endings) if endings else ceiling
    return text[snippet_start:snippet_end].strip()


def _clean_text(value: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#160;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _symbol(value: str) -> str:
    cleaned = value.strip().upper()
    if not cleaned:
        raise ValueError("Symbol is required.")
    return cleaned

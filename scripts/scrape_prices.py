from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


DIGIKALA_URL = (
    "https://www.digikala.com/wealth/my-assets/"
    "?utm_source=dk&utm_medium=undersearchbutton&utm_campaign=fixbutton"
)

ISIGNAL_URL = "https://isignal.ir/gold-currency/usdollar/"

ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_FILE = ROOT_DIR / "public" / "data" / "prices.json"

TIMEOUT_SECONDS = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
}


def now_iso() -> str:
    """Return the current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def normalize_digits(value: str) -> str:
    """Convert Persian and Arabic digits to English digits."""
    translation = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )
    return value.translate(translation)


def clean_text(value: str) -> str:
    """Normalize whitespace and digits."""
    value = normalize_digits(value)
    value = value.replace("\u200c", " ")
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def parse_number(value: str) -> int | None:
    """
    Extract an integer number from Persian/English formatted text.

    Supports values such as:
    2,314,600
    ۲٬۳۱۴٬۶۰۰
    2314600 ریال
    """
    if not value:
        return None

    value = clean_text(value)
    value = value.replace(",", "")
    value = value.replace("٬", "")
    value = value.replace(".", "")
    value = value.replace(" ", "")

    match = re.search(r"\d+", value)
    if not match:
        return None

    return int(match.group(0))


def fetch_html(url: str) -> str:
    """Download a public HTML page."""
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response.text


def all_visible_text(html: str) -> str:
    """Extract readable page text."""
    soup = BeautifulSoup(html, "lxml")

    for element in soup(["script", "style", "noscript", "svg"]):
        element.decompose()

    return clean_text(soup.get_text(" ", strip=True))


def extract_usd_price(html: str) -> int | None:
    """
    Extract USD price in rial from the iSignal page.

    The page currently exposes text similar to:
    دلار ... 2,314,600 ریال
    """
    text = all_visible_text(html)

    patterns = [
        r"دلار.{0,180}?([\d۰-۹٠-٩][\d۰-۹٠-٩,٬.]*)\s*ریال",
        r"قیمت روز دلار.{0,180}?([\d۰-۹٠-٩][\d۰-۹٠-٩,٬.]*)\s*ریال",
        r"([\d۰-۹٠-٩][\d۰-۹٠-٩,٬.]*)\s*ریال",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = parse_number(match.group(1))
            if value and value > 100_000:
                return value

    return None


def extract_number_near_keywords(
    text: str,
    keywords: list[str],
    max_distance: int = 500,
) -> int | None:
    """
    Find a number close to one of the supplied keywords.

    This is intentionally conservative because the source page may change
    its HTML structure.
    """
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), text, flags=re.IGNORECASE):
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + max_distance)
            nearby = text[start:end]

            number_matches = re.findall(
                r"[\d۰-۹٠-٩][\d۰-۹٠-٩,٬.]*",
                nearby,
            )

            candidates: list[int] = []

            for raw_number in number_matches:
                number = parse_number(raw_number)
                if number is not None:
                    candidates.append(number)

            if candidates:
                # Prefer a realistic price over tiny labels or dates.
                realistic = [
                    number for number in candidates
                    if 1 <= number <= 10_000_000_000
                ]

                if realistic:
                    return realistic[-1]

    return None


def extract_gold_and_silver(html: str) -> tuple[int | None, int | None]:
    """
    Extract gold and silver per-milligram prices.

    The source page is dynamic and may change. This function searches
    the rendered HTML/text for labels and nearby numbers.

    IMPORTANT:
    The unit must be verified from the page. If the source returns toman,
    set SOURCE_UNIT_TO_RIAL_MULTIPLIER to 10.
    """
    text = all_visible_text(html)

    gold = extract_number_near_keywords(
        text,
        [
            "طلای ۱۸ عیار",
            "طلای 18 عیار",
            "طلا",
        ],
    )

    silver = extract_number_near_keywords(
        text,
        [
            "نقره ۹۹۹",
            "نقره 999",
            "نقره",
        ],
    )

    return gold, silver


def load_existing_data() -> dict[str, Any]:
    """Load the previous JSON so temporary source failures do not erase data."""
    if not OUTPUT_FILE.exists():
        return {}

    try:
        return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def make_price_record(
    value: int | None,
    previous: dict[str, Any],
    source: str,
    unit: str = "ریال",
) -> dict[str, Any]:
    """
    Preserve the previous valid value if a source temporarily fails.
    """
    if value is not None:
        return {
            "value": value,
            "unit": unit,
            "source": source,
            "status": "fresh",
        }

    previous_value = previous.get("value")

    if previous_value is not None:
        return {
            "value": previous_value,
            "unit": previous.get("unit", unit),
            "source": previous.get("source", source),
            "status": "stale",
        }

    return {
        "value": None,
        "unit": unit,
        "source": source,
        "status": "unavailable",
    }


def main() -> int:
    previous = load_existing_data()

    result: dict[str, Any] = {
        "fetched_at": now_iso(),
        "currency": "ریال",
        "sources": {
            "gold_silver": DIGIKALA_URL,
            "usd": ISIGNAL_URL,
        },
        "prices": {},
        "errors": [],
    }

    try:
        digikala_html = fetch_html(DIGIKALA_URL)
        gold, silver = extract_gold_and_silver(digikala_html)

        result["prices"]["gold_18k_milligram"] = make_price_record(
            gold,
            previous.get("prices", {}).get("gold_18k_milligram", {}),
            DIGIKALA_URL,
        )

        result["prices"]["silver_999_milligram"] = make_price_record(
            silver,
            previous.get("prices", {}).get("silver_999_milligram", {}),
            DIGIKALA_URL,
        )

    except Exception as exc:
        result["errors"].append(f"دیجی‌کالا: {type(exc).__name__}: {exc}")

        old_prices = previous.get("prices", {})

        result["prices"]["gold_18k_milligram"] = make_price_record(
            None,
            old_prices.get("gold_18k_milligram", {}),
            DIGIKALA_URL,
        )

        result["prices"]["silver_999_milligram"] = make_price_record(
            None,
            old_prices.get("silver_999_milligram", {}),
            DIGIKALA_URL,
        )

    try:
        isignal_html = fetch_html(ISIGNAL_URL)
        usd = extract_usd_price(isignal_html)

        result["prices"]["usd"] = make_price_record(
            usd,
            previous.get("prices", {}).get("usd", {}),
            ISIGNAL_URL,
        )

    except Exception as exc:
        result["errors"].append(f"سیگنال: {type(exc).__name__}: {exc}")

        result["prices"]["usd"] = make_price_record(
            None,
            previous.get("prices", {}).get("usd", {}),
            ISIGNAL_URL,
        )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))

    fresh_count = sum(
        1
        for item in result["prices"].values()
        if item.get("status") == "fresh"
    )

    if fresh_count == 0:
        print("هیچ قیمت جدیدی استخراج نشد.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

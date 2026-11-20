"""Validated versioned JSON shared by the three agents."""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
import unicodedata

STORES = {name: name + ".ru" for name in ("goldapple", "randewoo", "letu", "rivegauche")}
SUPPLIER = "application.delice-opt.ru"
STATUSES = {"confirmed", "manual_review", "no_card", "no_access", "blocked", "unavailable"}
STOCK = {"in_stock", "out_of_stock", "unknown"}


def clock():
    return datetime.now(timezone.utc)


def timestamp(value):
    time = datetime.fromisoformat(value)
    if time.utcoffset() is None:
        raise ValueError("checked_at must include a time zone")
    return time


def number(value, *, zero=False):
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Expected a decimal number") from exc
    if not amount.is_finite() or amount < 0 or (not zero and amount == 0):
        raise ValueError("Expected a finite positive number")
    return amount


def normalized(value):
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


@dataclass(frozen=True)
class Product:
    brand: str
    name: str
    concentration: str
    volume_ml: str
    package: str
    configuration: str = ""

    def __post_init__(self):
        for value in (self.brand, self.name):
            if not isinstance(value, str) or not normalized(value):
                raise ValueError("Brand and name must be explicit")
        number(self.volume_ml)
        if self.concentration not in {"EDP", "EDT", "EXTRAIT", "PARFUM", "EDC"}:
            raise ValueError("Unknown concentration")
        if self.package not in {"retail", "tester", "set", "sample", "refill"}:
            raise ValueError("Unknown package")
        if not isinstance(self.configuration, str) or (self.package in {"set", "sample", "refill"} and not normalized(self.configuration)):
            raise ValueError("Explicit configuration required for this package")

    def key(self):
        return (normalized(self.brand), normalized(self.name), self.concentration,
                number(self.volume_ml), self.package, normalized(self.configuration))


def check_url(source, url):
    parts = urlsplit(url)
    domain = STORES.get(source, source)
    hosts = {domain, "www." + domain} if source in STORES else {SUPPLIER}
    if (parts.scheme != "https" or parts.hostname not in hosts or parts.username is not None
            or parts.password is not None or parts.port not in (None, 443)
            or parts.query or parts.fragment or parts.path in ("", "/")):
        raise ValueError("Expected a clean HTTPS product URL on " + domain)
    return url


def observation(product, source, status, *, url="", price=None, stock="unknown",
                reason="", variant="", checked_at=None, evidence=""):
    return {
        "product": product.__dict__, "source": source, "status": status, "url": url,
        "price_rub": str(number(price)) if price is not None else None,
        "availability": stock, "reason": reason, "variant": variant,
        "checked_at": checked_at or clock().isoformat(), "evidence": evidence,
    }


def validate(payload, agent):
    if agent not in {"competitors", "supplier"}:
        raise ValueError("Unknown producer")
    if type(payload.get("version")) is not int or payload["version"] != 1 or payload.get("agent") != agent:
        raise ValueError("Unexpected exchange version or producer")
    rows = payload["observations"]
    if not isinstance(rows, list):
        raise ValueError("observations must be an array")
    expected = set(STORES) if agent == "competitors" else {SUPPLIER}
    for row in rows:
        Product(**row["product"])
        timestamp(row["checked_at"])
        if row["source"] not in expected or row["status"] not in STATUSES or row["availability"] not in STOCK:
            raise ValueError("Invalid source, status or availability")
        if any(not isinstance(row[field], str) for field in ("url", "reason", "variant", "evidence")):
            raise ValueError("Invalid observation text")
        if row["url"]:
            check_url(row["source"], row["url"])
        if row["status"] == "confirmed":
            if not row["url"] or row["evidence"] not in {"automatic", "manual"}:
                raise ValueError("Confirmation requires a URL and evidence")
            if row["evidence"] == "manual" and not row["reason"].strip():
                raise ValueError("Manual confirmation requires a note")
            number(row["price_rub"])
        elif row["price_rub"] is not None:
            raise ValueError("Unconfirmed rows cannot carry a price")
    return rows


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, agent, rows):
    payload = {"version": 1, "agent": agent, "observations": rows}
    validate(payload, agent)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def targets(path):
    for item in load(path)["products"]:
        yield Product(**item["product"]), item.get("urls", {})


def confirmations(path, agent):
    if not path:
        return []
    rows = validate(load(path), agent)
    if any(row["status"] != "confirmed" or row["evidence"] != "manual" for row in rows):
        raise ValueError("Confirmations must contain only manually attested observations")
    return rows


def stock(value):
    value = normalized(value).removeprefix("https://schema.org/").removeprefix("http://schema.org/")
    if value in {"instock", "in_stock", "в наличии"}:
        return "in_stock"
    if value in {"outofstock", "soldout", "out_of_stock", "нет в наличии"}:
        return "out_of_stock"
    return "unknown"

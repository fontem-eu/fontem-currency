"""
Minor-unit currency codes
=========================
Some markets quote share prices in a currency's minor unit, and the
market-data sources label them with codes that are not ISO 4217: Yahoo
says GBp for London (pence), ZAc for Johannesburg (cents), ILA for Tel
Aviv (agorot); London's own lists say GBX. Upper-casing GBp reads it as
GBP, a hundred times the price.

``minor_unit_codes.json`` names the ISO currency behind each code. The
factor comes from the ISO 4217 list (``iso4217-list-one.xml``, as SIX,
the ISO 4217 maintenance agency, publishes it): a currency with 2 minor
units is divided into 100.
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET  # nosec B405 - our own bundled copy of the ISO list
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

ISO_LIST = "iso4217-list-one.xml"
CODES_FILE = "minor_unit_codes.json"


def iso_minor_units(path: Path) -> tuple[dict[str, int], str | None]:
    """{ISO code: minor units} from the ISO 4217 list, plus the list's
    publication date. Codes without a number of minor units (gold, SDR:
    "N.A.") are left out."""
    root = ET.parse(path).getroot()  # nosec B314 - bundled file, not input
    out: dict[str, int] = {}
    for entry in root.iter("CcyNtry"):
        code, units = entry.findtext("Ccy"), entry.findtext("CcyMnrUnts")
        if code and units and units.isdigit():
            out[code] = int(units)
    return out, root.get("Pblshd")


class MinorUnits:
    """Which codes are a currency's minor unit, and how small it is."""

    def __init__(self, codes: dict[str, str], minor_units: dict[str, int]) -> None:
        self._iso = set(minor_units)
        self._exact: dict[str, tuple[str, Decimal]] = {}
        for code, currency in codes.items():
            units = minor_units.get(currency, 0)
            if units <= 0:
                logger.error("minor-unit code %s names %s, which the ISO list "
                             "doesn't divide; ignored", code, currency)
                continue
            self._exact[code] = (currency, Decimal(1).scaleb(-units))
        self._any_case = {code.upper(): unit for code, unit in self._exact.items()}

    @classmethod
    def load(cls, data_dir: Path) -> "MinorUnits":
        minor_units, published = iso_minor_units(data_dir / ISO_LIST)
        with open(data_dir / CODES_FILE, encoding="utf-8") as fh:
            codes = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
        logger.info("ISO 4217 list published %s: %d currencies; %d minor-unit codes",
                    published, len(minor_units), len(codes))
        return cls(codes, minor_units)

    def lookup(self, code: str) -> tuple[str, Decimal] | None:
        """(ISO currency, scale) when ``code`` is a minor unit, where
        amount x scale is the amount in that currency; None otherwise.

        The exact spelling wins (GBp is pence, GBP is pounds). Any other
        spelling of a code counts too (GBX, gbx), unless it is itself an
        ISO code: gbp stays pounds.
        """
        code = code.strip()
        if code in self._exact:
            return self._exact[code]
        upper = code.upper()
        if upper in self._iso:
            return None
        return self._any_case.get(upper)

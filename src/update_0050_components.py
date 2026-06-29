"""
Update the local 0050 component file from Yuanta's ETF API.

This script is designed for GitHub Actions:
- use the same JSON bridge API that the Yuanta ETF page uses
- validate the result before overwriting the local file
- exit non-zero on suspicious data so the previous committed file is kept
"""

from __future__ import annotations

import csv
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "0050_component_paste.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://www.yuantaetfs.com/product/detail/0050/ratio",
}


def is_listed(code: str) -> bool:
    return bool(re.fullmatch(r"[1-8]\d{3}", str(code).strip()))


def to_number(value) -> float:
    text = str(value or "").replace(",", "").replace("%", "").strip()
    text = re.sub(r"[^\d.\-]", "", text)
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_components(payload: dict) -> list[dict]:
    rows = payload.get("FundWeights", {}).get("StockWeights", [])
    components: list[dict] = []

    for row in rows if isinstance(rows, list) else []:
        code = str(row.get("code", "")).strip()
        if not is_listed(code):
            continue

        name = str(row.get("name", "")).strip()
        if not name:
            continue

        quantity = to_number(row.get("qty"))
        weight = to_number(row.get("weights"))

        components.append({
            "code": code,
            "name": name,
            "quantity": int(quantity),
            "weight": round(weight, 4),
        })

    unique = {item["code"]: item for item in components}
    return list(unique.values())


def validate_components(components: list[dict]) -> None:
    if not 45 <= len(components) <= 55:
        raise ValueError(f"unexpected component count: {len(components)}")

    missing_quantity = [item["code"] for item in components if item["quantity"] <= 0]
    if missing_quantity:
        raise ValueError(f"missing quantity for: {', '.join(missing_quantity[:5])}")

    weights = [item["weight"] for item in components if item["weight"] > 0]
    if len(weights) < 40:
        raise ValueError("too many missing component weights")

    total_weight = sum(weights)
    if not 80 <= total_weight <= 105:
        raise ValueError(f"suspicious total weight: {total_weight:.2f}")


def fetch_latest_components() -> tuple[str, list[dict]]:
    session = requests.Session()
    session.headers.update(HEADERS)

    url = "https://etfapi.yuantaetfs.com/ectranslation/api/bridge"
    params = {
        "APIType": "ETFAPI",
        "CompanyName": "YUANTAFUNDS",
        "PageName": "/product/detail/0050/ratio",
        "DeviceId": "null",
        "FuncId": "PCF/Daily",
        "AppName": "ETF",
        "Device": "3",
        "Platform": "ETF",
        "ticker": "0050",
    }
    print("Trying Yuanta ETF API: PCF/Daily ticker=0050")
    response = session.get(url, params=params, timeout=30)
    if response.status_code in (403, 429):
        raise RuntimeError(f"Yuanta ETF API blocked with HTTP {response.status_code}")
    response.raise_for_status()

    payload = response.json()
    components = parse_components(payload)
    validate_components(components)

    pcf = payload.get("PCF", {})
    raw_date = str(pcf.get("trandate") or pcf.get("upddate") or "").strip()
    if re.fullmatch(r"\d{8}", raw_date):
        data_date = datetime.strptime(raw_date, "%Y%m%d").strftime("%Y-%m-%d")
    else:
        data_date = raw_date or "unknown date"
    return data_date, components


def output_headers() -> list[str]:
    if OUT_PATH.exists():
        first_line = OUT_PATH.read_text(encoding="utf-8-sig").splitlines()[0]
        headers = first_line.split("\t")
        if len(headers) == 4:
            return headers
    return ["code", "name", "quantity", "weight"]


def write_components(components: list[dict]) -> None:
    headers = output_headers()
    key_order = ["code", "name", "quantity", "weight"]
    with OUT_PATH.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file, delimiter="\t", lineterminator="\n")
        writer.writerow(headers)
        for item in components:
            writer.writerow([item[key] for key in key_order])


def main() -> int:
    try:
        data_date, components = fetch_latest_components()
        write_components(components)
        print(f"Updated {OUT_PATH.name}: {len(components)} components from {data_date}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

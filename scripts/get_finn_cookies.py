"""Hent FINN-cookies for Pristips.

Apner FINN Pristips i en synlig nettleser.
Brukeren logger inn manuelt (email + kode).
Etter innlogging lagres cookies til .finn_cookies.json.

Bruk: python scripts/get_finn_cookies.py
Kjores manuelt naar cookies utloper (typisk hver 2-4 uke).
"""

import json
import asyncio
from pathlib import Path

COOKIE_FILE = Path(__file__).parent.parent / ".finn_cookies.json"
PRISTIPS_URL = "https://www.finn.no/mobility/insights/price-valuation"


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(PRISTIPS_URL)

        print("\n" + "=" * 60)
        print("LOGG INN PAA FINN I NETTLESEREN SOM AAPNET SEG")
        print("Etter innlogging, soek opp en bil for aa verifisere.")
        print("Trykk ENTER her naar du er ferdig.")
        print("=" * 60 + "\n")

        input()

        cookies = await context.cookies()
        with open(COOKIE_FILE, "w") as f:
            json.dump(cookies, f, indent=2)

        print(f"Lagret {len(cookies)} cookies til {COOKIE_FILE}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

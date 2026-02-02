"""
Biluppgifter.se API Client - Hybrid Version
Använder Playwright för Cloudflare-bypass + curl_cffi för snabb datahämtning.
"""

import os
import re
import json
import time
import threading
from curl_cffi import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()


class CookieManager:
    """Hanterar cookies automatiskt med Playwright."""

    def __init__(self):
        self.cookies = {}
        self.last_refresh = 0
        self.lock = threading.Lock()
        self.min_refresh_interval = 60  # Minst 60 sek mellan refreshes

    def get_cookies(self) -> dict:
        """Hämta giltiga cookies, refresha om nödvändigt."""
        with self.lock:
            if not self.cookies:
                self._refresh_cookies()
            return self.cookies.copy()

    def force_refresh(self):
        """Tvinga cookie-refresh (t.ex. efter 403)."""
        with self.lock:
            now = time.time()
            if now - self.last_refresh < self.min_refresh_interval:
                print(f"[CookieManager] Skipping refresh, last was {int(now - self.last_refresh)}s ago")
                return
            self._refresh_cookies()

    def _refresh_cookies(self):
        """Öppna Playwright och hämta nya cookies."""
        print("[CookieManager] Refreshing cookies with Playwright...")

        try:
            with sync_playwright() as p:
                # Starta browser med stealth-inställningar
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--disable-dev-shm-usage',
                        '--no-sandbox',
                    ]
                )

                context = browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    viewport={'width': 1920, 'height': 1080},
                    locale='sv-SE',
                )

                # Ta bort webdriver-flaggan
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                """)

                page = context.new_page()

                # Navigera till biluppgifter.se
                print("[CookieManager] Navigating to biluppgifter.se...")
                page.goto('https://biluppgifter.se/', wait_until='networkidle', timeout=30000)

                # Vänta på att Cloudflare-challenge löses
                time.sleep(3)

                # Kolla om vi fortfarande är på Cloudflare
                if 'challenge' in page.url or 'cloudflare' in page.content().lower():
                    print("[CookieManager] Waiting for Cloudflare challenge...")
                    page.wait_for_load_state('networkidle', timeout=15000)
                    time.sleep(2)

                # Extrahera cookies
                cookies = context.cookies()
                self.cookies = {
                    'theme': 'dark',
                }

                for cookie in cookies:
                    name = cookie['name']
                    value = cookie['value']
                    if name in ['session', 'cf_clearance', '.AspNetCore.Antiforgery.KXUQR4SkAeM']:
                        self.cookies[name] = value
                        print(f"[CookieManager] Got cookie: {name[:20]}...")

                self.last_refresh = time.time()
                browser.close()

                print(f"[CookieManager] Cookies refreshed successfully! Got {len(self.cookies)} cookies")

        except Exception as e:
            print(f"[CookieManager] Error refreshing cookies: {e}")
            # Fallback till env-variabler om Playwright misslyckas
            self.cookies = {
                'theme': 'dark',
                'session': os.getenv('BILUPPGIFTER_SESSION', ''),
                'cf_clearance': os.getenv('BILUPPGIFTER_CF_CLEARANCE', ''),
                '.AspNetCore.Antiforgery.KXUQR4SkAeM': os.getenv('BILUPPGIFTER_ANTIFORGERY', ''),
            }


# Global cookie manager
cookie_manager = CookieManager()


class BiluppgifterClient:
    """Biluppgifter.se client med automatisk cookie-hantering."""

    BASE_URL = "https://biluppgifter.se"

    def __init__(self):
        self.headers = {
            "referer": f"{self.BASE_URL}/",
            "accept-language": "sv-SE,sv;q=0.9,en;q=0.8",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        self._retry_count = 0
        self._max_retries = 2

    def _fetch_page(self, path: str) -> str:
        """Hämta sida med curl_cffi, auto-refresh cookies vid behov."""
        cookies = cookie_manager.get_cookies()

        try:
            r = requests.get(
                f"{self.BASE_URL}{path}",
                impersonate="chrome",
                cookies=cookies,
                headers=self.headers,
                timeout=15,
            )

            if r.status_code == 403:
                if self._retry_count < self._max_retries:
                    self._retry_count += 1
                    print(f"[BiluppgifterClient] Got 403, refreshing cookies (attempt {self._retry_count})...")
                    cookie_manager.force_refresh()
                    return self._fetch_page(path)  # Retry
                else:
                    self._retry_count = 0
                    raise PermissionError(
                        "Cloudflare blockerade requesten efter flera försök. "
                        "Testa igen om en stund."
                    )

            self._retry_count = 0  # Reset on success

            if r.status_code != 200:
                raise ConnectionError(f"HTTP {r.status_code} från biluppgifter.se")

            return r.text

        except requests.exceptions.Timeout:
            raise ConnectionError("Timeout vid anslutning till biluppgifter.se")

    # ── Parsers ──────────────────────────────────────────────

    def _parse_label_values(self, soup: BeautifulSoup) -> dict:
        sections = {}
        for section in soup.find_all("section"):
            h2 = section.find("h2")
            name = h2.get_text(strip=True) if h2 else section.get("id", "")
            if not name:
                continue
            section_data = {}
            for li in section.find_all("li"):
                label_el = li.find("span", class_="label")
                value_el = li.find("span", class_="value")
                if label_el and value_el:
                    label = label_el.get_text(strip=True)
                    value = value_el.get_text(strip=True)
                    if label and value and not value.startswith(("Hämta ", "Jämför ", "Räkna ")):
                        section_data[label] = value
            if section_data:
                sections[name] = section_data
        return sections

    def _parse_title(self, soup: BeautifulSoup) -> str:
        title = soup.find("title")
        if title:
            return title.get_text(strip=True).replace(" - Biluppgifter.se", "").strip()
        return ""

    def _parse_owner_from_vehicle(self, soup: BeautifulSoup) -> dict:
        """Extrahera ägarinfo + ägarhistorik från fordonssidan."""
        owner_section = soup.find("section", id="owner-history")
        if not owner_section:
            return {}

        result = {}

        # Nuvarande ägare (från paragraf)
        intro_p = owner_section.find("p")
        if intro_p:
            text = intro_p.get_text(strip=True)
            result["summary"] = text

            # Extrahera ägarlänk
            owner_link = intro_p.find("a", href=re.compile(r"/brukare/"))
            if owner_link:
                result["current_owner"] = {
                    "name": owner_link.get_text(strip=True),
                    "profile_id": owner_link["href"].strip("/").split("/")[-1],
                    "profile_url": owner_link["href"],
                }
                # Stad
                em_tags = intro_p.find_all("em")
                for em in em_tags:
                    t = em.get_text(strip=True)
                    if t.startswith("från "):
                        result["current_owner"]["city"] = t.replace("från ", "")

        # Ägarhistorik
        owners_list = []
        for li in owner_section.find_all("li", class_=True):
            owner_type_class = [c for c in li.get("class", []) if c in ("person", "company", "rental", "dealer")]
            owner_type = owner_type_class[0] if owner_type_class else "unknown"

            info_div = li.find("div", class_="info")
            if not info_div:
                continue

            h3 = info_div.find("h3")
            p = info_div.find("p")
            if not h3:
                continue

            # Datum
            date_span = h3.find("span", class_="numb")
            date = date_span.get_text(strip=True) if date_span else ""

            # Typ
            type_text = h3.get_text(strip=True).replace(date, "").strip()

            # Namn och info
            entry = {
                "type": type_text,
                "owner_class": owner_type,
                "date": date,
            }

            if p:
                link = p.find("a", href=re.compile(r"/brukare/"))
                if link:
                    entry["name"] = link.get_text(strip=True)
                    entry["profile_id"] = link["href"].strip("/").split("/")[-1]
                    entry["profile_url"] = link["href"]
                entry["details"] = p.get_text(strip=True)

            owners_list.append(entry)

        if owners_list:
            result["history"] = owners_list

        return result

    def _parse_owner_profile(self, soup: BeautifulSoup) -> dict:
        """Extrahera all data från ägarprofil-sidan (/brukare/{id}/)."""
        result = {}

        # Parse action-boxes for address and phone (new structure)
        for action_box in soup.find_all("div", class_="action-box"):
            strong = action_box.find("strong")
            if not strong:
                continue
            label = strong.get_text(strip=True).lower()

            # Adress box (green)
            if label == "adress":
                paragraphs = action_box.find_all("p")
                if len(paragraphs) >= 1:
                    result["address"] = paragraphs[0].get_text(strip=True)
                if len(paragraphs) >= 2:
                    postal_text = paragraphs[1].get_text(strip=True)
                    postal_match = re.match(r"^(\d{5})\s+(.+)$", postal_text)
                    if postal_match:
                        result["postal_code"] = postal_match.group(1)
                        result["postal_city"] = postal_match.group(2)

            # Telefon box (brown)
            elif label == "telefon":
                phone_p = action_box.find("p")
                if phone_p:
                    phone_text = phone_p.get_text(strip=True)
                    if phone_text and "inga" not in phone_text.lower():
                        result["phone"] = phone_text

        # Hitta info-sektionen för namn, ålder etc
        for section in soup.find_all("section"):
            h2 = section.find("h2")
            section_text = section.get_text(strip=True)

            # Personinfo (huvudsektion)
            if "privatperson" in section_text or "bor i" in section_text or "år gammal" in section_text:
                for p in section.find_all("p"):
                    text = p.get_text(strip=True)

                    # Namn, ålder, stad
                    m = re.search(r"^(.+?), en (\w+) som är (\d+) år.+bor i (.+?),", text)
                    if m:
                        result["name"] = m.group(1)
                        result["person_type"] = m.group(2)
                        result["age"] = int(m.group(3))
                        result["city"] = m.group(4)
                        continue

                    # Personnummer
                    pnr = re.search(r"(\d{8}-\d{4})", text)
                    if pnr:
                        result["personnummer"] = pnr.group(1)
                        continue

            # Personens fordon
            name = h2.get_text(strip=True) if h2 else ""
            if "fordon" in name.lower() and "andra" not in name.lower():
                result["vehicles"] = self._parse_vehicle_links(section)

            # Andra fordon på adressen
            if "andra fordon" in name.lower():
                no_vehicles = section.find("p")
                if no_vehicles and "inga" in no_vehicles.get_text(strip=True).lower():
                    result["address_vehicles"] = []
                else:
                    result["address_vehicles"] = self._parse_vehicle_links(section)

        return result

    def _parse_vehicle_links(self, element) -> list:
        """Extrahera fordonslänkar från ett HTML-element."""
        vehicles = []
        for a in element.find_all("a", href=re.compile(r"/fordon/")):
            text = a.get_text(strip=True)
            href = a["href"]
            regnr_match = re.search(r"/fordon/([a-zA-Z0-9]+)", href)
            regnr = regnr_match.group(1).upper() if regnr_match else ""
            if regnr and text:
                vehicles.append({
                    "regnr": regnr,
                    "description": text,
                    "url": href,
                })
        return vehicles

    def _parse_vehicle_table(self, html: str) -> list:
        """Extrahera fordon från HTMX-laddad tabell."""
        soup = BeautifulSoup(html, "html.parser")
        vehicles = []
        for row in soup.find_all("tr", class_=True):
            cells = row.find_all("td")
            if not cells:
                continue
            link = row.find("a", href=re.compile(r"/fordon/"))
            if not link:
                continue
            regnr_match = re.search(r"/fordon/([a-zA-Z0-9]+)", link["href"])
            regnr = regnr_match.group(1).upper() if regnr_match else ""
            model = link.get_text(strip=True)
            entry = {"regnr": regnr, "model": model}

            for i, td in enumerate(cells):
                text = td.get_text(strip=True)

                if "mono" in td.get("class", []):
                    entry["regnr"] = text.upper()

                color_div = td.find("div", class_="color")
                if color_div:
                    entry["color"] = text

                if re.match(r"^\d{4}$", text):
                    entry["year"] = int(text)

                if re.match(r"^\d{4}-\d{2}(-\d{2})?$", text):
                    entry["date_acquired"] = text
                elif "år sedan" in text or "mån sedan" in text:
                    entry["ownership_time"] = text

            row_classes = row.get("class", [])
            if "itrafik" in row_classes:
                entry["status"] = "I Trafik"
            elif "avregistrerad" in row_classes:
                entry["status"] = "Avregistrerad"
            elif "avstalld" in row_classes:
                entry["status"] = "Avställd"

            vehicles.append(entry)
        return vehicles

    def _fetch_htmx_vehicles(self, profile_path: str, profile_id: str) -> list:
        """Hämta fordonslista via HTMX-endpoint."""
        html = self._fetch_page(f"{profile_path}?handler=vehicles&currentPage=1")
        return self._parse_vehicle_table(html)

    def _parse_mileage_history(self, soup: BeautifulSoup) -> list:
        """Extrahera mätarställningshistorik från fordonssidan."""
        history = []
        meter_section = soup.find("section", id="meter-history")
        if not meter_section:
            return history

        for h3 in meter_section.find_all("h3"):
            text = h3.get_text(strip=True)
            if not text.startswith("Besiktning"):
                continue

            span = h3.find("span", class_="numb")
            if not span:
                continue

            span_text = span.get_text(strip=True)
            match = re.match(r"([\d\s]+)\s*mil(\d{4}-\d{2}-\d{2})", span_text)
            if match:
                mileage_str = match.group(1).replace(" ", "")
                mileage = int(mileage_str)
                date = match.group(2)
                history.append({
                    "date": date,
                    "mileage_mil": mileage,
                    "mileage_km": mileage * 10,
                    "type": "besiktning"
                })

        return history

    # ── Public API ───────────────────────────────────────────

    def lookup(self, regnr: str) -> dict:
        """Sök fordonsdata med registreringsnummer."""
        regnr = regnr.strip().upper()
        html = self._fetch_page(f"/fordon/{regnr.lower()}/")
        soup = BeautifulSoup(html, "html.parser")

        return {
            "regnr": regnr,
            "page_title": self._parse_title(soup),
            "data": self._parse_label_values(soup),
            "owner": self._parse_owner_from_vehicle(soup),
            "mileage_history": self._parse_mileage_history(soup),
        }

    def lookup_owner_profile(self, profile_id: str) -> dict:
        """Hämta ägarprofil med personnummer, adress, fordon och adressfordon."""
        profile_path = f"/brukare/{profile_id}/"
        html = self._fetch_page(profile_path)
        soup = BeautifulSoup(html, "html.parser")

        result = {
            "profile_id": profile_id,
            **self._parse_owner_profile(soup),
        }

        try:
            result["vehicles"] = self._fetch_htmx_vehicles(profile_path, profile_id)
        except Exception:
            pass

        return result

    def lookup_owner_by_regnr(self, regnr: str) -> dict:
        """Hämta ägarprofil via registreringsnummer."""
        vehicle = self.lookup(regnr)
        owner = vehicle.get("owner", {})
        current = owner.get("current_owner", {})
        profile_id = current.get("profile_id")

        if not profile_id:
            return {
                "regnr": regnr,
                "error": "Kunde inte hitta ägarlänk för detta fordon.",
            }

        profile = self.lookup_owner_profile(profile_id)
        return {
            "regnr": regnr,
            "vehicle_title": vehicle.get("page_title", ""),
            "owner_profile": profile,
            "owner_history": owner.get("history", []),
        }

    def lookup_address_vehicles(self, regnr: str) -> dict:
        """Hämta alla fordon registrerade på samma adress."""
        owner_data = self.lookup_owner_by_regnr(regnr)
        profile = owner_data.get("owner_profile", {})

        return {
            "regnr": regnr,
            "owner": profile.get("name", ""),
            "address": profile.get("address", ""),
            "postal_code": profile.get("postal_code", ""),
            "postal_city": profile.get("postal_city", ""),
            "owner_vehicles": profile.get("vehicles", []),
            "address_vehicles": profile.get("address_vehicles", []),
        }


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    USAGE = """Användning:
  python biluppgifter.py vehicle <regnr>         Fordonsdata
  python biluppgifter.py owner <regnr>           Ägarinfo via regnr
  python biluppgifter.py profile <profile_id>    Ägarprofil direkt
  python biluppgifter.py address <regnr>         Alla fordon på ägarens adress
  python biluppgifter.py refresh                 Tvinga cookie-refresh

Exempel:
  python biluppgifter.py vehicle XBD134
  python biluppgifter.py refresh"""

    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "refresh":
        cookie_manager.force_refresh()
        print("Cookies refreshed!")
        sys.exit(0)

    if len(sys.argv) < 3:
        print(USAGE)
        sys.exit(1)

    client = BiluppgifterClient()
    arg = sys.argv[2]

    try:
        if cmd == "vehicle":
            data = client.lookup(arg)
        elif cmd == "owner":
            data = client.lookup_owner_by_regnr(arg)
        elif cmd == "profile":
            data = client.lookup_owner_profile(arg)
        elif cmd == "address":
            data = client.lookup_address_vehicles(arg)
        else:
            print(f"Okänt kommando: {cmd}")
            print(USAGE)
            sys.exit(1)

        print(json.dumps(data, indent=2, ensure_ascii=False))
    except (PermissionError, ConnectionError) as e:
        print(f"FEL: {e}", file=sys.stderr)
        sys.exit(1)

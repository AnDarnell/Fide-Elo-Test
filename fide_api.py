"""
Hämtning av partier från ratings.fide.com, byggd för att vara snäll mot FIDE.

- Lång slumpad paus mellan varje anrop (MIN_DELAY-MAX_DELAY sekunder)
- Tak på antal anrop per hämtning (MAX_REQUESTS_PER_FETCH)
- Tomt svar / 403 / 429 ger ett tydligt fel och ingen automatisk omförsökning
- Hämtade sidor sparas på disk, så en avbruten körning fortsätter där den slutade
  och samma period aldrig hämtas två gånger
"""
import json
import math
import random
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://ratings.fide.com"
RATING_TYPES = {0: "Standard", 1: "Rapid", 2: "Blitz"}

# --- Hur försiktiga vi är ---
MIN_DELAY = 6.0                # sekunder mellan anrop (minst)
MAX_DELAY = 12.0               # sekunder mellan anrop (högst)
MAX_REQUESTS_PER_FETCH = 15    # absolut tak per hämtning
PERIODS_TTL_DAYS = 7           # hur länge periodlistan återanvänds
CACHE_DIR = Path(".fide_page_cache")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://ratings.fide.com/",
}

_session = requests.Session()
_session.headers.update(HEADERS)

_last_request = 0.0
_request_count = 0


class FideError(Exception):
    """Allmänt fel vid hämtning från FIDE."""


class FideBlocked(FideError):
    """FIDE verkar strypa eller blockera oss (tomt svar, 403 eller 429)."""


def _ajax_headers(fide_id: str, period_date: str | None = None, t: int = 0) -> dict:
    if period_date:
        referer = f"{BASE_URL}/calculations.phtml?id_number={fide_id}&period={period_date}&rating={t}"
    else:
        referer = f"{BASE_URL}/profile/{fide_id}/calculations"
    return {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Referer": referer,
    }


def reset_request_counter() -> None:
    global _request_count
    _request_count = 0


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """
    Enda stället som pratar med FIDE. Väntar mellan anrop, räknar anrop och
    avbryter direkt om FIDE svarar konstigt (inga omförsök, för att inte förvärra en strypning).
    """
    global _last_request, _request_count

    if _request_count >= MAX_REQUESTS_PER_FETCH:
        raise FideError(
            f"Nådde taket på {MAX_REQUESTS_PER_FETCH} anrop. "
            "Redan hämtade perioder är sparade, så en ny körning fortsätter där den slutade."
        )

    if _last_request:
        wait = random.uniform(MIN_DELAY, MAX_DELAY) - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)

    _request_count += 1
    try:
        resp = _session.request(method, url, timeout=20, **kwargs)
    except requests.RequestException as e:
        raise FideError(f"Nätverksfel mot FIDE: {e}") from e
    finally:
        _last_request = time.monotonic()

    if resp.status_code in (403, 429):
        raise FideBlocked(f"FIDE svarade {resp.status_code} (blockerad eller strypt).")
    if resp.status_code >= 400:
        raise FideError(f"FIDE svarade {resp.status_code} för {url}")
    if not resp.text.strip():
        raise FideBlocked(
            "Tomt svar från FIDE. Det brukar betyda att din IP är strypt. "
            "Sluta hämta en stund och försök igen senare."
        )
    return resp


# ---------------------------------------------------------------------------
# Perioder
# ---------------------------------------------------------------------------

def _periods_cache_path(fide_id: str) -> Path:
    return CACHE_DIR / f"{fide_id}_periods.json"


def _load_periods_cache(fide_id: str) -> list[dict] | None:
    path = _periods_cache_path(fide_id)
    if not path.exists():
        return None
    age_days = (time.time() - path.stat().st_mtime) / 86400
    if age_days > PERIODS_TTL_DAYS:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_periods_cache(fide_id: str, periods: list[dict]) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        _periods_cache_path(fide_id).write_text(json.dumps(periods), encoding="utf-8")
    except OSError:
        pass


def _dedupe_sort(periods: list[dict], years: int) -> list[dict]:
    cutoff = (date.today() - timedelta(days=365 * years)).isoformat()
    seen = set()
    unique = []
    for p in periods:
        key = (p["period_date"], p["t"])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    unique.sort(key=lambda p: p["period_date"], reverse=True)
    return [p for p in unique if p["period_date"] >= cutoff]


def get_periods(fide_id: str, years: int = 3, use_cache: bool = True) -> list[dict]:
    """
    Hämtar de senaste ratingperioderna (första batchen, ett enda anrop).
    Äldre perioder hämtas separat med get_older_periods() och bara om det behövs.
    """
    if use_cache:
        cached = _load_periods_cache(fide_id)
        if cached is not None:
            return _dedupe_sort(cached, years)

    resp = _request(
        "GET",
        f"{BASE_URL}/a_calculations.phtml?event={fide_id}",
        headers=_ajax_headers(fide_id),
    )
    periods = _parse_periods(resp.text)
    if not periods:
        raise FideError(
            "Hittade inga ratingperioder. Fel FIDE-ID, spelaren saknar ratade partier, "
            "eller så har FIDE ändrat sidan."
        )
    _save_periods_cache(fide_id, periods)
    return _dedupe_sort(periods, years)


def get_older_periods(fide_id: str, years: int = 3) -> list[dict]:
    """Ett POST-anrop som laddar resten av perioderna ('Check more periods')."""
    resp = _request(
        "POST",
        f"{BASE_URL}/a_calculations.phtml",
        data={"action": "2", "plr_id": fide_id},
        headers=_ajax_headers(fide_id),
    )
    return _dedupe_sort(_parse_periods(resp.text), years)


def _parse_periods_table(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    periods = []
    table = soup.find("table", {"id": "profile-table2"})
    if not table:
        return periods

    tbody = table.find("tbody")
    if not tbody:
        return periods

    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        period_text = cells[0].get_text(strip=True)

        for cell, t in zip(cells[1:4], [0, 1, 2]):
            link = cell.find("a")
            if not link:
                continue
            href = link.get("href", "")
            match = re.search(r"period=(\d{4}-\d{2}-\d{2})", href)
            if not match:
                continue
            periods.append({
                "period_text": period_text,
                "period_date": match.group(1),
                "t": t,
                "type_name": RATING_TYPES[t],
            })

    return periods


_LINK_RE = re.compile(r"period=\s*(\d{4}-\d{2}-\d{2})\s*&(?:amp;)?rating=\s*(\d)")


def _parse_periods_fragment(html: str) -> list[dict]:
    """
    Tolkar lösa <tr>-rader (POST-svaret) med regex på länkarna. Ratingtypen
    läses ur länken (rating=0/1/2), så trasig HTML runt cellerna spelar ingen roll.
    """
    periods = []
    for chunk in re.split(r"<tr\b", html)[1:]:
        m = re.search(r"<td[^>]*>\s*(\d{4}-[A-Za-z]{3})\s*</td>", chunk)
        period_text = m.group(1) if m else ""
        for lm in _LINK_RE.finditer(chunk):
            t = int(lm.group(2))
            if t in RATING_TYPES:
                periods.append({
                    "period_text": period_text,
                    "period_date": lm.group(1),
                    "t": t,
                    "type_name": RATING_TYPES[t],
                })
    return periods


def _parse_periods(html: str) -> list[dict]:
    """Först tabellvarianten (första batchen), annars lösa rader (POST-svaret)."""
    return _parse_periods_table(html) or _parse_periods_fragment(html)


# ---------------------------------------------------------------------------
# Spelarprofil
# ---------------------------------------------------------------------------

def get_player_profile(fide_id: str) -> dict:
    """Fetch the player's name and published ratings from their FIDE profile."""
    # Profile lookup is a separate, single-request operation from a game fetch,
    # which may already have used its full request budget.
    reset_request_counter()
    resp = _request("GET", f"{BASE_URL}/profile/{fide_id}")
    soup = BeautifulSoup(resp.text, "html.parser")

    name = None
    for selector in ("h1", ".profile-top-title", ".player-title"):
        heading = soup.select_one(selector)
        if heading:
            candidate = heading.get_text(" ", strip=True)
            if candidate and fide_id not in candidate:
                name = candidate
                break

    if not name:
        og_title = soup.find("meta", property="og:title")
        title = og_title.get("content", "") if og_title else ""
        if not title:
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
        title = re.sub(r"\s*[-|:]?\s*FIDE Profile\s*$", "", title, flags=re.IGNORECASE).strip()
        if title and fide_id not in title and "FIDE Ratings" not in title:
            name = title

    ratings = {}
    text_nodes = [text.strip() for text in soup.stripped_strings if text.strip()]
    for index, text in enumerate(text_nodes):
        rating_type = text.upper().strip(" .:")
        if rating_type not in ("STANDARD", "RAPID", "BLITZ"):
            continue
        for previous in reversed(text_nodes[max(0, index - 6):index]):
            match = re.fullmatch(r"(\d{3,4})", previous.replace(",", ""))
            if match:
                ratings[rating_type.title()] = int(match.group(1))
                break

    return {"name": name, "ratings": ratings}


# ---------------------------------------------------------------------------
# Partier
# ---------------------------------------------------------------------------

def _games_cache_path(fide_id: str, period_date: str, t: int) -> Path:
    return CACHE_DIR / f"{fide_id}_{period_date}_{t}.json"


def get_games_for_period(fide_id: str, period_date: str, t: int) -> list[dict]:
    """Partier för en period och ratingtyp. Läser från disk om perioden redan är hämtad."""
    path = _games_cache_path(fide_id, period_date, t)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    url = f"{BASE_URL}/a_indv_calculation.php?id_number={fide_id}&rating_period={period_date}&t={t}"
    resp = _request("GET", url, headers=_ajax_headers(fide_id, period_date, t))
    games = _parse_games(resp.text, period_date, RATING_TYPES[t])

    if not games:
        # Sidan kom, men inget kunde tolkas. Spara HTML:en för felsökning.
        try:
            Path("fide_debug_last.html").write_text(resp.text, encoding="utf-8")
        except OSError:
            pass
        raise FideError(
            f"Hämtade sidan för {period_date}, men kunde inte tolka några partier "
            "(ändrad layout?). Sidan sparades i fide_debug_last.html."
        )

    try:
        CACHE_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(games), encoding="utf-8")
    except OSError:
        pass
    return games


def _parse_games(html: str, period_date: str, type_name: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    games = []

    current_tournament = "Unknown"
    current_date = period_date

    body = soup.find("body") or soup

    for elem in body.children:
        if not hasattr(elem, "name") or elem.name is None:
            continue

        # Turneringsrubrik
        if elem.name == "div" and "default_div_full" in (elem.get("class") or []):
            title_link = elem.find("a", class_="head1")
            if title_link:
                current_tournament = title_link.get_text(strip=True)
                date_spans = elem.find_all("span", class_="dates_span")
                if date_spans:
                    current_date = date_spans[0].get_text(strip=True)
            continue

        # Parti-tabell
        if elem.name == "table" and "calc_table" in (elem.get("class") or []):
            for row in elem.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 10:
                    continue
                if "list4" not in (cells[0].get("class") or []):
                    continue
                if cells[0].get("colspan"):
                    continue

                name_raw = cells[0].get_text(strip=True)
                title = cells[1].get_text(strip=True)
                opp_rating_raw = cells[3].get_text(strip=True)
                score_raw = cells[5].get_text(strip=True)
                n_raw = cells[6].get_text(strip=True)
                k_chg_raw = cells[9].get_text(strip=True)

                try:
                    opp_rating = int(opp_rating_raw.strip())
                except ValueError:
                    opp_rating = 0

                try:
                    score = float(score_raw.strip())
                except ValueError:
                    score = 0.0

                try:
                    n_games = int(n_raw.strip())
                except ValueError:
                    n_games = 1

                try:
                    rating_change = float(k_chg_raw.strip())
                except ValueError:
                    rating_change = 0.0

                if not name_raw:
                    continue

                games.append({
                    "period": period_date,
                    "type": type_name,
                    "tournament": current_tournament,
                    "date": current_date,
                    "opponent": name_raw,
                    "title": title,
                    "opp_rating": opp_rating,
                    "score": score,
                    "n_games": n_games,
                    "result": _score_to_result(score, n_games),
                    "rating_change": rating_change,
                })

    return games


def _score_to_result(score: float, n_games: int) -> str:
    if n_games == 1:
        if score == 1.0:
            return "Win"
        elif score == 0.5:
            return "Draw"
        else:
            return "Loss"
    return f"{score}/{n_games}"


# ---------------------------------------------------------------------------
# Huvudfunktion
# ---------------------------------------------------------------------------

def fetch_last_n_games(fide_id: str, n: int, rating_type: int, progress=None) -> dict:
    """
    Hämtar partier period för period (nyast först) tills n partier finns.
    Äldre perioder laddas bara om de nyaste inte räcker.

    progress(antal_partier, mål, text) anropas om den anges.
    Returnerar {"periods": [...], "games": [...], "periods_fetched": int}.
    """
    reset_request_counter()

    def report(msg: str) -> None:
        if progress:
            progress(len(games), n, msg)

    games: list[dict] = []
    done: set[str] = set()
    warnings: list[str] = []
    older_loaded = False

    report("Hämtar periodlista...")
    periods = get_periods(fide_id)

    while True:
        todo = [p for p in periods if p["t"] == rating_type and p["period_date"] not in done]

        if not todo:
            if older_loaded or len(games) >= n:
                break
            report("Hämtar äldre perioder...")
            older = get_older_periods(fide_id)
            older_loaded = True
            known = {(p["period_date"], p["t"]) for p in periods}
            new = [p for p in older if (p["period_date"], p["t"]) not in known]
            if not new:
                warnings.append(
                    "Kunde inte hämta äldre perioder (anropet för 'Check more periods' gav "
                    "inga nya perioder). Det kan finnas fler partier än de som visas."
                )
            periods = periods + new
            periods.sort(key=lambda p: p["period_date"], reverse=True)
            continue

        p = todo[0]
        report(f"Hämtar {p['period_text']}...")
        games.extend(get_games_for_period(fide_id, p["period_date"], rating_type))
        done.add(p["period_date"])
        report(f"{len(games)} partier hittade")

        if len(games) >= n:
            break

    return {"periods": periods, "games": games, "periods_fetched": len(done), "warnings": warnings}


# ---------------------------------------------------------------------------
# Performance rating
# ---------------------------------------------------------------------------

def calc_performance_rating(games: list[dict]) -> float | None:
    """Beräknar performance rating för en lista partier."""
    games = [g for g in games if g["opp_rating"] > 0]  # hoppa över oratade motståndare
    if not games:
        return None

    total_games = sum(g["n_games"] for g in games)
    if total_games == 0:
        return None

    total_score = sum(g["score"] for g in games)
    avg_opp = sum(g["opp_rating"] * g["n_games"] for g in games) / total_games

    percentage = total_score / total_games
    percentage = max(0.01, min(0.99, percentage))  # undvik oändliga värden

    return round(avg_opp + _percentage_to_dp(percentage), 1)


def _percentage_to_dp(p: float) -> float:
    """Poängprocent -> ratingdifferens enligt FIDE:s dp-tabell (approximation)."""
    table = [
        (1.00, 800), (0.99, 677), (0.98, 589), (0.97, 538), (0.96, 501),
        (0.95, 470), (0.94, 444), (0.93, 422), (0.92, 401), (0.91, 383),
        (0.90, 366), (0.89, 351), (0.88, 336), (0.87, 322), (0.86, 309),
        (0.85, 296), (0.84, 284), (0.83, 273), (0.82, 262), (0.81, 251),
        (0.80, 240), (0.79, 230), (0.78, 220), (0.77, 211), (0.76, 202),
        (0.75, 193), (0.74, 184), (0.73, 175), (0.72, 166), (0.71, 158),
        (0.70, 149), (0.69, 141), (0.68, 133), (0.67, 125), (0.66, 117),
        (0.65, 110), (0.64, 102), (0.63, 95),  (0.62, 87),  (0.61, 80),
        (0.60, 72),  (0.59, 65),  (0.58, 57),  (0.57, 50),  (0.56, 43),
        (0.55, 36),  (0.54, 29),  (0.53, 21),  (0.52, 14),  (0.51, 7),
        (0.50, 0),
    ]

    if p < 0.50:
        return -_percentage_to_dp(1.0 - p)

    for threshold, dp in table:
        if p >= threshold:
            return dp

    return 0


# ---------------------------------------------------------------------------
# Viktad rullande prestationsrating
# ---------------------------------------------------------------------------

def sort_newest_first(games: list[dict]) -> list[dict]:
    """Senaste först: period, därefter turneringens startdatum. Stabil sortering, så
    ordningen inom en turnering (sidans egen ordning) behålls."""
    return sorted(games, key=lambda g: (g["period"], g["date"]), reverse=True)


def _dp_interp(p: float) -> float:
    """Som _percentage_to_dp men med linjär interpolation mellan tabellens procentsteg."""
    p = max(0.01, min(0.99, p))
    if p < 0.5:
        return -_dp_interp(1.0 - p)
    x = round(p * 100, 9)
    lo = math.floor(x)
    hi = min(lo + 1, 100)
    d_lo = _percentage_to_dp(lo / 100)
    d_hi = _percentage_to_dp(hi / 100)
    return d_lo + (x - lo) * (d_hi - d_lo)


def calc_weighted_performance(
    games_newest_first: list[dict],
    window: int = 30,
    w_newest: float = 1.0,
    w_oldest: float = 0.2,
) -> dict | None:
    """
    Prestationsrating över de `window` senaste partierna. Vikten faller linjärt från
    w_newest (senaste partiet) till w_oldest (parti nummer `window` bakåt).
    Har spelaren färre än `window` partier behåller senaste partiet vikt 1,0 och
    de äldre får de vikter deras position ger. Motståndare utan rating hoppas över.
    """
    # Expand grouped rating rows into individual games before applying the
    # rolling window, so a window of 30 always means at most 30 actual games.
    individual_games = []
    for g in games_newest_first:
        if g["opp_rating"] <= 0:
            continue
        n = max(1, int(g["n_games"]))
        score_per_game = g["score"] / n
        individual_games.extend((g["opp_rating"], score_per_game) for _ in range(n))
        if len(individual_games) >= window:
            individual_games = individual_games[:window]
            break

    num = den = opp_sum = w2 = 0.0
    n_rated = len(individual_games)
    for k, (opp_rating, score) in enumerate(individual_games):
        w = w_newest - (w_newest - w_oldest) * k / (window - 1) if window > 1 else w_newest
        num += w * score
        den += w
        opp_sum += w * opp_rating
        w2 += w * w
    if den == 0:
        return None
    pct = num / den
    avg_opp = opp_sum / den
    return {
        "rating": round(avg_opp + _dp_interp(pct), 1),
        "pct": pct,
        "avg_opp": avg_opp,
        "n_games": n_rated,
        "n_eff": den * den / w2,   # effektivt antal partier efter viktning
    }

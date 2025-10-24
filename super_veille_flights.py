#!/usr/bin/env python3
"""
Super-veille vols Paris ↔ Osaka (+ Cebu opportunité) — **Amadeus Only**

Ce script interroge l'API Amadeus Self‑Service (Flight Offers Search v2)
pour :
1) Paris ↔ Osaka avec **séjour EXACT 90 jours**
2) Osaka → Cebu (J+90) → Paris (J+14)

Règles appliquées :
- ≤ 1 escale par trajet
- Durée totale par vol < 25 h
- Bagage soute **inclus**
- Toutes compagnies (low-cost OK sur les segments Cebu aussi)
- TOP 3 à chaque exécution
- Alertes : ≤ 650 € ; ou 651–700 € **seulement si** itinéraire « exceptionnel »

🔧 Configuration via variables d'environnement (.env) :
  AMADEUS_CLIENT_ID
  AMADEUS_CLIENT_SECRET
  AMADEUS_ENV=test|prod            # par défaut: test
  CURRENCY=EUR                     # par défaut: EUR
  DISCORD_WEBHOOK_URL=...          # (optionnel) pour recevoir des alertes

Dépendances : requests, python-dateutil, python-dotenv (optionnel)

© 2025 — Gabarit final pour Maxime.
"""

import os
import json
import logging
import time
from typing import List, Dict, Any, Optional, Tuple
from datetime import date, timedelta

import requests
from dateutil.relativedelta import relativedelta

try:
    # charge .env si présent (facilite les tests locaux)
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

# --- Paramètres généraux ---
PARIS_AIRPORTS = ["CDG", "ORY"]
OSAKA_AIRPORTS = ["KIX", "ITM", "UKB"]
CEBU_AIRPORT = "CEB"
CURRENCY = os.getenv("CURRENCY", "EUR")

MAX_STOPS = 1
MAX_DURATION_HOURS = 25
BAGGAGE_REQUIRED = True

THRESHOLD_MAIN = 650.0      # alerte directe
EXCEPTION_MIN = 651.0
EXCEPTION_MAX = 700.0

DEPART_WINDOW_START = date(2026, 1, 1)
DEPART_WINDOW_END   = date(2026, 1, 15)

# --- Amadeus ---
AMADEUS_ENV = os.getenv("AMADEUS_ENV", "test").lower()
AMADEUS_HOST = "https://test.api.amadeus.com" if AMADEUS_ENV != "prod" else "https://api.amadeus.com"
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

if not (AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET):
    logging.warning("⚠️ AMADEUS_CLIENT_ID/SECRET non définis — définissez-les dans votre .env")

# --- Utilitaires ---

def daterange(start: date, end: date):
    for n in range((end - start).days + 1):
        yield start + timedelta(days=n)


def generate_paris_osaka_exact90_pairs():
    pairs = []
    for d0 in daterange(DEPART_WINDOW_START, DEPART_WINDOW_END):
        d1 = d0 + relativedelta(days=+90)
        if date(2026, 4, 1) <= d1 <= date(2026, 4, 15):
            pairs.append((d0, d1))
    return pairs


def exceptional_itinerary(duration_hours: float, layovers: List[float], premium: bool) -> bool:
    # Itinéraire « exceptionnel » : durée 15–18h ou correspondances très courtes (45–150 min) ou compagnie premium.
    short_layover = any(0.75 <= l <= 2.5 for l in layovers)
    return (15.0 <= duration_hours <= 18.0) or short_layover or premium


# --- Amadeus client ---
class Amadeus:
    def __init__(self):
        self.host = AMADEUS_HOST
        self.client_id = AMADEUS_CLIENT_ID
        self.client_secret = AMADEUS_CLIENT_SECRET
        self._token = None

    def _auth(self):
        url = f"{self.host}/v1/security/oauth2/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        r = requests.post(url, data=data, timeout=20)
        r.raise_for_status()
        self._token = r.json().get("access_token")

    def _headers(self):
        if not self._token:
            self._auth()
        return {"Authorization": f"Bearer {self._token}"}

    def search_offers(self, origin: str, dest: str, depart: date, ret: Optional[date]=None, adults: int=2) -> List[Dict[str, Any]]:
    	url = f"{self.host}/v2/shopping/flight-offers"
    	params = {
	        "originLocationCode": origin,
	        "destinationLocationCode": dest,
	        "departureDate": depart.isoformat(),
        	"adults": str(adults),
        	"currencyCode": CURRENCY,
        	"max": "20",  # 50 -> 20 pour limiter la charge
    	}
    	if ret:
	        params["returnDate"] = ret.isoformat()
	    r = self._safe_get(url, params)
	    return r.json().get("data", [])

	
	def _safe_get(self, url, params, max_retries=6):
    attempt = 0
    while True:
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code == 429:
            attempt += 1
            if attempt > max_retries:
                resp.raise_for_status()
            retry_after = resp.headers.get("Retry-After")
            try:
                wait_s = min(60, int(retry_after)) if retry_after else min(60, 2 ** attempt)
            except Exception:
                wait_s = min(60, 2 ** attempt)
            time.sleep(wait_s)
            continue
        resp.raise_for_status()
        return resp


# --- Vérifications d'une offre ---

def parse_duration_hours(iso: str) -> float:
    # Durée ISO8601: PTxxHyyM
    if not iso or not iso.startswith("PT"):
        return 0.0
    iso = iso.replace("PT", "").lower()
    h, m = 0.0, 0.0
    if "h" in iso:
        parts = iso.split("h")
        h = float(parts[0]) if parts[0] else 0.0
        tail = parts[1] if len(parts) > 1 else ""
        if "m" in tail:
            m = float(tail.split("m")[0] or 0)
    elif "m" in iso:
        m = float(iso.replace("m", "") or 0)
    return h + m/60.0


def offer_meets_rules(offer: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    metrics = {"total_hours": 0.0, "stops": 0, "bag_included": False, "premium": False, "layovers": []}
    try:
        its = offer.get("itineraries", [])
        total_h = 0.0
        max_stops = 0
        layovers: List[float] = []
        for itin in its:
            segs = itin.get("segments", [])
            max_stops = max(max_stops, max(0, len(segs) - 1))
            total_h += parse_duration_hours(itin.get("duration", "PT0H"))
            # si données horaires dispo, on pourrait calculer les vraies correspondances
            if len(segs) > 1:
                layovers.append(1.5)
        # bagage inclus ?
        bag_included = False
        for tp in offer.get("travelerPricings", []):
            for f in tp.get("fareDetailsBySegment", []):
                inc = f.get("includedCheckedBags")
                if isinstance(inc, dict) and (inc.get("quantity") or 0) > 0:
                    bag_included = True
        premium = any(code in {"AF","KL","AY","NH","JL","QR","SQ","CX","LH","LX"} for code in offer.get("validatingAirlineCodes", []))
        metrics.update({"total_hours": total_h, "stops": max_stops, "bag_included": bag_included, "premium": premium, "layovers": layovers})
        ok = (max_stops <= MAX_STOPS) and (total_h < MAX_DURATION_HOURS) and ((not BAGGAGE_REQUIRED) or bag_included)
        return ok, metrics
    except Exception:
        logging.exception("Erreur dans offer_meets_rules")
        return False, metrics


def get_price(offer: Dict[str, Any]) -> float:
    try:
        return float(offer.get("price", {}).get("total", 1e9))
    except Exception:
        return 1e9


def pick_top3(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(offers, key=get_price)[:3]


def compact(offer: Dict[str, Any], m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "price": offer.get("price", {}).get("total"),
        "currency": offer.get("price", {}).get("currency"),
        "carriers": ",".join(offer.get("validatingAirlineCodes", [])),
        "stops": m.get("stops"),
        "hours": round(m.get("total_hours", 0.0), 1),
        "bag_included": bool(m.get("bag_included")),
        "premium": bool(m.get("premium")),
    }


# --- Logique principale ---

def run_once() -> Dict[str, Any]:
    ama = Amadeus()

    # 1) PAR ↔ OSA (90 jours exacts)
    paris_osaka_pairs = generate_paris_osaka_exact90_pairs()
    valid_osaka: List[Dict[str, Any]] = []

    for d0, d1 in paris_osaka_pairs:
        for o in PARIS_AIRPORTS:
            for d in OSAKA_AIRPORTS:
                for _ in range(1):
                    offers = ama.search_offers(o, d, d0, d1, adults=2)
                    for off in offers:
                        ok, metr = offer_meets_rules(off)
                        if ok:
                            off["_metrics"] = metr
                            valid_osaka.append(off)

    top3_osaka_offers = pick_top3(valid_osaka)
    top3_osaka = [compact(o, o.get("_metrics", {})) for o in top3_osaka_offers]

    # 2) OSA → CEB (J+90) → PAR (J+14)
    valid_cebu_combo: List[Dict[str, Any]] = []
    for d_osaceb in daterange(date(2026, 4, 1), date(2026, 4, 15)):
        d_cebpar = d_osaceb + relativedelta(days=+14)
        osa_ceb_valid: List[Dict[str, Any]] = []
        ceb_par_valid: List[Dict[str, Any]] = []
        # Osaka -> Cebu
        for o in OSAKA_AIRPORTS:
            offers_a = ama.search_offers(o, CEBU_AIRPORT, d_osaceb, None, adults=2)
            for off in offers_a:
                ok, metr = offer_meets_rules(off)
                if ok:
                    off["_metrics"] = metr
                    osa_ceb_valid.append(off)
        # Cebu -> Paris (vers CDG/ORY)
        for p in PARIS_AIRPORTS:
            offers_b = ama.search_offers(CEBU_AIRPORT, p, d_cebpar, None, adults=2)
            for off in offers_b:
                ok, metr = offer_meets_rules(off)
                if ok:
                    off["_metrics"] = metr
                    ceb_par_valid.append(off)
        # Combinaisons
        for a in osa_ceb_valid:
            for b in ceb_par_valid:
                total = get_price(a) + get_price(b)
                valid_cebu_combo.append({
                    "total": round(total, 2),
                    "currency": a.get("price", {}).get("currency", CURRENCY),
                    "osa_ceb": compact(a, a.get("_metrics", {})),
                    "ceb_par": compact(b, b.get("_metrics", {})),
                    "dates": {"osa_ceb": d_osaceb.isoformat(), "ceb_par": d_cebpar.isoformat()},
                })

    top3_cebu = sorted(valid_cebu_combo, key=lambda x: x.get("total", 1e9))[:3]

    # 3) Alertes
    alerts = {"osaka": None, "cebu_combo": None}
    if top3_osaka:
        best = top3_osaka[0]
        price = float(best.get("price", 1e9))
        if price <= THRESHOLD_MAIN:
            alerts["osaka"] = {"reason": "≤650€", "best": best}
        elif EXCEPTION_MIN <= price <= EXCEPTION_MAX and exceptional_itinerary(best.get("hours", 99), [], best.get("premium", False)):
            alerts["osaka"] = {"reason": "651–700€ exceptionnel", "best": best}

    if top3_cebu and top3_osaka:
        ref = float(top3_osaka[0].get("price", 1e9)) + 300.0
        if float(top3_cebu[0].get("total", 1e9)) < ref:
            alerts["cebu_combo"] = {"best": top3_cebu[0], "vs_ref": ref}

    return {"top3_osaka": top3_osaka, "top3_cebu": top3_cebu, "alerts": alerts}


# --- Notifications (optionnel) ---

def notify_discord(payload: Dict[str, Any]):
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    content = ["**Super‑veille — Résultats**"]
    if payload.get("top3_osaka"):
        content.append("\n**TOP 3 Paris↔Osaka**")
        for i, x in enumerate(payload["top3_osaka"], 1):
            content.append(f"{i}. {x['price']} {x['currency']} — {x['carriers']} — {x['hours']}h, {x['stops']} stop(s), bag={x['bag_included']}")
    if payload.get("top3_cebu"):
        content.append("\n**TOP 3 Osaka→Cebu→Paris**")
        for i, x in enumerate(payload["top3_cebu"], 1):
            content.append(f"{i}. total {x['total']} {x['currency']} — dates {x['dates']['osa_ceb']} / {x['dates']['ceb_par']}")
    if payload.get("alerts", {}).get("osaka"):
        a = payload["alerts"]["osaka"]
        content.append(f"\n🔔 Alerte Osaka: {a['reason']} — {a['best']['price']} {a['best']['currency']}")
    if payload.get("alerts", {}).get("cebu_combo"):
        a = payload["alerts"]["cebu_combo"]
        content.append(f"\n🔔 Alerte Cebu combo: total {a['best']['total']} < ref {a['vs_ref']:.2f}")
    try:
        requests.post(url, json={"content": "\n".join(content)[:1900]}, timeout=15)
    except Exception:
        logging.exception("Envoi Discord échoué")


if __name__ == "__main__":
	if not os.getenv("AMADEUS_CLIENT_ID") or not os.getenv("AMADEUS_CLIENT_SECRET"):
    	raise SystemExit("FATAL: AMADEUS_CLIENT_ID/SECRET manquants. Ajoute-les dans tes secrets GitHub Actions.")
    out = run_once()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    notify_discord(out)

# -----------------------------
# FICHIER .env EXEMPLE (à créer)
# -----------------------------
# AMADEUS_CLIENT_ID=xxxx
# AMADEUS_CLIENT_SECRET=xxxx
# AMADEUS_ENV=test
# CURRENCY=EUR
# DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# -----------------------------
# CRON (4x/jour)
# -----------------------------
# 0 0,6,12,18 * * * /chemin/projet/.venv/bin/python /chemin/projet/super_veille_flights.py >> /chemin/projet/logs/veille.log 2>&1
"""Skuplja oglase za nove stanove u Zadru s web stranica zadarskih agencija.

Rezultat se sprema u podaci/oglasi.json, koji stranica učitava.
Pokretanje: python skupljac/skupi.py
Za probu nad spremljenim HTML-om: python skupljac/skupi.py --uzorci <mapa>
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = "NovogradnjaZadarBot/0.1 (+https://github.com/steph1804/novi-stanovi-zadar)"
PAUZA = 2.0  # sekunde između zahtjeva prema istoj agenciji
MAX_STRANICA = 15
IZLAZ = Path(__file__).resolve().parent.parent / "podaci" / "oglasi.json"

NOVOGRADNJA = re.compile(r"novogradnj|nova gradnja|novoizgra|u izgradnji|new ?construction|newly built|new[ -]?ly constructed|new building|new apartment|neubau|izgradnja|završetak|zavrsetak|completion", re.I)
NAJAM = re.compile(r"najam|rent|vermiet|miete", re.I)

# Riječi imaju prednost pred oznakama stanova poput "S1", koje su često samo broj stana u zgradi.
SOBE = [
    (re.compile(r"garsonijer|studio", re.I), 0),
    (re.compile(r"jednosob|one[- ]bedroom", re.I), 1),
    (re.compile(r"dvosob|two[- ]bedroom", re.I), 2),
    (re.compile(r"trosob|three[- ]bedroom", re.I), 3),
    (re.compile(r"četverosob|cetverosob|four[- ]bedroom", re.I), 4),
    (re.compile(r"\b1[ -]?sob", re.I), 1),
    (re.compile(r"\b2[ -]?sob", re.I), 2),
    (re.compile(r"\b3[ -]?sob", re.I), 3),
    (re.compile(r"\b4[ -]?sob", re.I), 4),
]

# Mjesta u okolici koja nisu dio grada Zadra, iako ih agencije često navode uz "Zadar".
OKOLICA = re.compile(r"\b(privlaka|vir|nin|sukošan|sukosan|bibinje|petrčane|petrcane|zaton|sv\.? filip|biograd|pakoštane|"
                     r"posedarje|ražanac|razanac|poličnik|policnik|murvica|ugljan|pašman|pasman|dugi otok|"
                     r"turanj|sveti petar|tkon|starigrad|novigrad|kukljica)\b", re.I)


def broj(tekst):
    """'489,900 €' -> 489900, '146.00 m2' -> 146.0; vraća None ako nema broja."""
    if not tekst:
        return None
    m = re.search(r"\d[\d.,]*", tekst)
    if not m:
        return None
    s = m.group(0)
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        return int(re.sub(r"[.,]", "", s))
    if re.fullmatch(r"\d+[.,]\d{1,2}", s):
        return float(s.replace(",", "."))
    return int(re.sub(r"[.,]", "", s))


def sobe_iz_naslova(naslov):
    for uzorak, n in SOBE:
        if uzorak.search(naslov):
            return n
    return None


KVARTOVI = ["Arbanasi", "Belafuža", "Bili Brig", "Bokanjac", "Novi Bokanjac", "Borik", "Brodarica", "Crvene kuće",
            "Diklo", "Dračevac", "Gaženica", "Jazine", "Kožino", "Puntamika", "Plovanija", "Poluotok", "Relja",
            "Smiljevac", "Sinjoretovo", "Vidikovac", "Višnjik", "Voštarnica", "Ploča", "Ričina", "Maslina"]


def bez_kvacica(t):
    return t.lower().translate(str.maketrans("čćžšđ", "cczsd"))


def kvart_iz_naslova(naslov):
    n = bez_kvacica(naslov)
    for k in sorted(KVARTOVI, key=len, reverse=True):
        if re.search(r"\b" + re.escape(bez_kvacica(k)) + r"\b", n):
            return k
    return ""


def ocisti(t):
    return re.sub(r"\s+", " ", t or "").strip()


# --- predlošci agencijskih stranica -------------------------------------------

def parsiraj_property_box(html, baza):
    """Predložak s karticama .property-box (npr. Artis nekretnine)."""
    soup = BeautifulSoup(html, "html.parser")
    for box in soup.select(".property-box"):
        a = box.select_one(".title a")
        if not a:
            continue
        footer = box.select_one(".footer")
        yield {
            "naslov": ocisti(a.get_text()),
            "url": urljoin(baza, a["href"]),
            "cijena": broj(ocisti(box.select_one(".price-box").get_text()) if box.select_one(".price-box") else ""),
            "povrsina": broj(footer.select_one(".col-5").get_text()) if footer and footer.select_one(".col-5") else None,
            "lokacija": ocisti(box.select_one(".location").get_text()) if box.select_one(".location") else "",
            "vrsta": ocisti(footer.select_one(".text-right").get_text()) if footer and footer.select_one(".text-right") else "",
            "oznake": [ocisti(b.get_text()) for b in box.select(".listing-badges span")],
        }


def parsiraj_at_property(html, baza):
    """Predložak s karticama .at-property-item (npr. Top Lokacija nekretnine)."""
    soup = BeautifulSoup(html, "html.parser")
    for item in soup.select(".at-property-item"):
        a = item.select_one(".at-property-location h4 a")
        if not a:
            continue
        lis = item.select(".at-property-dis li")
        yield {
            "naslov": ocisti(a.get_text()),
            "url": urljoin(baza, a["href"]),
            "cijena": broj(item.select_one(".at-property-img h4").get_text()) if item.select_one(".at-property-img h4") else None,
            "povrsina": broj(lis[0].get_text()) if lis else None,
            "lokacija": ocisti(item.select_one(".at-property-location p").get_text()) if item.select_one(".at-property-location p") else "",
            "vrsta": ocisti(lis[1].get_text()) if len(lis) > 1 else "",
            "oznake": [ocisti(h.get_text()) for h in item.select(".at-property-img h5")],
        }


AGENCIJE = [
    {
        "id": "artis",
        "naziv": "Artis nekretnine",
        "web": "https://www.artis-nekretnine.hr",
        "popis": "https://www.artis-nekretnine.hr/stanovi-zadar-novogradnja",
        "parser": parsiraj_property_box,
        "samo_novogradnja": True,  # popis je već kategorija novogradnje
        "uzorak": "artis-lista.html",
    },
    {
        "id": "toplokacija",
        "naziv": "Top Lokacija nekretnine",
        "web": "https://www.toplokacija-nekretnine.com",
        "popis": "https://www.toplokacija-nekretnine.com/stanovi",
        "parser": parsiraj_at_property,
        "samo_novogradnja": False,
        "uzorak": "toplok-pocetna.html",
    },
]


def preuzmi(sesija, url):
    r = sesija.get(url, timeout=30)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text


def stranice(sesija, agencija):
    """Prolazi kroz ?page=1..N dok stranica ne prestane donositi nove oglase."""
    vidjeni = set()
    for n in range(1, MAX_STRANICA + 1):
        url = agencija["popis"] + (f"?page={n}" if n > 1 else "")
        html = preuzmi(sesija, url)
        novi = [o for o in agencija["parser"](html, agencija["web"]) if o["url"] not in vidjeni]
        if not novi:
            return
        for o in novi:
            vidjeni.add(o["url"])
            yield o
        time.sleep(PAUZA)


def normaliziraj(o, agencija, sada):
    lok = [d.strip() for d in o["lokacija"].split(",")]
    grad = lok[0] if lok else ""
    kvart = lok[1] if len(lok) > 1 else kvart_iz_naslova(o["naslov"])
    tekst = o["naslov"] + " " + " ".join(o["oznake"])
    cijena = o["cijena"] if isinstance(o["cijena"], (int, float)) and o["cijena"] > 1000 else None
    povrsina = o["povrsina"] if isinstance(o["povrsina"], (int, float)) and o["povrsina"] > 10 else None
    return {
        "id": agencija["id"] + ":" + o["url"].rstrip("/").rsplit("/", 1)[-1],
        "naslov": o["naslov"],
        "url": o["url"],
        "agencija": agencija["naziv"],
        "agencija_web": agencija["web"],
        "grad": grad,
        "kvart": kvart,
        "cijena": cijena,
        "povrsina": povrsina,
        "cijena_m2": round(cijena / povrsina) if cijena and povrsina else None,
        "sobe": sobe_iz_naslova(o["naslov"]),
        "projekt": povrsina is None and bool(re.search(r"kompleks|projekt|zgrad|development|apartments in|stanovi u", o["naslov"], re.I)),
        "novogradnja": agencija["samo_novogradnja"] or bool(NOVOGRADNJA.search(tekst)),
        "najam": bool(NAJAM.search(o["naslov"])),
        "stan": bool(re.search(r"stan|flat|wohnung", o["vrsta"], re.I)) or not o["vrsta"],
        "prvi_put_vidjen": sada,
        "zadnji_put_vidjen": sada,
    }


def zadrzi(o):
    return (o["stan"] and o["novogradnja"] and not o["najam"] and o["grad"].lower() == "zadar"
            and not OKOLICA.search(o["naslov"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uzorci", help="mapa sa spremljenim HTML-om umjesto preuzimanja")
    ap.add_argument("--izlaz", default=str(IZLAZ))
    args = ap.parse_args()

    sada = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    izlaz = Path(args.izlaz)
    stari = {}
    if izlaz.exists():
        try:
            stari = {o["id"]: o for o in json.loads(izlaz.read_text("utf-8"))["oglasi"]}
        except (ValueError, KeyError):
            pass

    sesija = requests.Session()
    sesija.headers["User-Agent"] = UA
    oglasi, izvori = [], []
    for ag in AGENCIJE:
        try:
            if args.uzorci:
                html = (Path(args.uzorci) / ag["uzorak"]).read_text("utf-8")
                sirovi = list(ag["parser"](html, ag["web"]))
            else:
                sirovi = list(stranice(sesija, ag))
            norm = [normaliziraj(o, ag, sada) for o in sirovi]
            zadrzani = [o for o in norm if zadrzi(o)]
            for o in zadrzani:
                if o["id"] in stari:
                    o["prvi_put_vidjen"] = stari[o["id"]].get("prvi_put_vidjen", sada)
            oglasi += zadrzani
            izvori.append({"agencija": ag["naziv"], "web": ag["web"], "ukupno": len(sirovi), "novogradnja": len(zadrzani), "greska": None})
            print(f"{ag['naziv']}: {len(sirovi)} oglasa, {len(zadrzani)} novogradnja u Zadru")
        except Exception as e:  # jedna agencija ne smije srušiti ostale
            # zadrži prošle oglase te agencije da stranica ne ostane prazna
            prosli = [o for o in stari.values() if o["agencija"] == ag["naziv"]]
            oglasi += prosli
            izvori.append({"agencija": ag["naziv"], "web": ag["web"], "ukupno": 0, "novogradnja": len(prosli), "greska": str(e)})
            print(f"{ag['naziv']}: GREŠKA {e}", file=sys.stderr)

    izlaz.parent.mkdir(parents=True, exist_ok=True)
    izlaz.write_text(json.dumps({"azurirano": sada, "izvori": izvori, "oglasi": oglasi}, ensure_ascii=False, indent=1), "utf-8")
    print(f"Spremljeno {len(oglasi)} oglasa u {izlaz}")


if __name__ == "__main__":
    main()

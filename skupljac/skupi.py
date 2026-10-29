"""Skuplja oglase za nove stanove u Zadru s web stranica zadarskih agencija.

Rezultat se sprema u podaci/oglasi.json, koji stranica učitava.
Pokretanje: python skupljac/skupi.py
Za probu nad spremljenim HTML-om: python skupljac/skupi.py --uzorci <mapa>
(u mapi treba biti datoteka <id agencije>.html za svaku agenciju)
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
MAX_DETALJA = 60  # najviše novih stranica oglasa po agenciji u jednom pokretanju
PODACI = Path(__file__).resolve().parent.parent / "podaci"
IZLAZ = PODACI / "oglasi.json"
PRIRUCNA = PODACI / "detalji.json"  # zapamćeni podaci sa stranica pojedinih oglasa
OVA_GODINA = datetime.now().year

NOVOGRADNJA = re.compile(r"novogradnj|nova gradnja|novoizgra|u izgradnji|new ?construction|newly built|new[ -]?ly constructed|"
                         r"new[ -]build|new building|new apartment|under construction|neubau|izgradnja|završetak|zavrsetak|completion", re.I)
NAJAM = re.compile(r"najam|\brent|vermiet|miete", re.I)
NIJE_STAN = re.compile(r"\b(kuća|kuca|vila|villa|house|zemljišt|zemljist|poslovni)", re.I)

# Riječi imaju prednost pred oznakama stanova poput "S1", koje su često samo broj stana u zgradi.
SOBE = [
    (re.compile(r"garsonijer|studio", re.I), 0),
    (re.compile(r"jednosob", re.I), 1),
    (re.compile(r"dvosob", re.I), 2),
    (re.compile(r"trosob", re.I), 3),
    (re.compile(r"četverosob|cetverosob", re.I), 4),
    (re.compile(r"\b1[.,]5[ -]?sob", re.I), 1),
    (re.compile(r"\b2[.,]5[ -]?sob", re.I), 2),
    (re.compile(r"\b3[.,]5[ -]?sob", re.I), 3),
    (re.compile(r"\b1[ -]?sob", re.I), 1),
    (re.compile(r"\b2[ -]?sob", re.I), 2),
    (re.compile(r"\b3[ -]?sob", re.I), 3),
    (re.compile(r"\b4[ -]?sob", re.I), 4),
]

# Mjesta u okolici koja nisu dio grada Zadra, iako ih agencije često navode uz "Zadar".
OKOLICA = re.compile(r"\b(privlaka|vir|nin|sukošan|sukosan|bibinje|petrčane|petrcane|zaton|sv\.? filip|sveti filip|biograd|"
                     r"pakoštane|pakostane|posedarje|ražanac|razanac|rtina|poličnik|policnik|murvica|ugljan|pašman|pasman|"
                     r"dugi otok|turanj|sveti petar|tkon|starigrad|novigrad|kukljica|vodice|povljana|pag|maslenica|"
                     r"jasenice|vinjerac)\b", re.I)

KVARTOVI = ["Arbanasi", "Belafuža", "Bili Brig", "Bokanjac", "Novi Bokanjac", "Borik", "Brodarica", "Bulevar", "Crvene kuće",
            "Diklo", "Dračevac", "Gaženica", "Jazine", "Kožino", "Mocire", "Puntamika", "Plovanija", "Poluotok", "Relja",
            "Smiljevac", "Sinjoretovo", "Skročini", "Vidikovac", "Višnjik", "Voštarnica", "Ploča", "Ričina", "Maslina",
            "Maraska park", "Branimir", "Diklovac", "Stanovi"]


def broj(tekst):
    """'489,900 €' -> 489900, '146.00 m2' -> 146.0; vraća None ako nema broja."""
    if not tekst:
        return None
    m = re.search(r"\d[\d.,]*", tekst)
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        return int(re.sub(r"[.,]", "", s))
    if re.fullmatch(r"\d+[.,]\d{1,2}", s):
        return float(s.replace(",", "."))
    return int(re.sub(r"[.,]", "", s))


def zadnja_cijena(tekst):
    """Kod sniženja agencije pišu staru pa novu cijenu; uzima se zadnja."""
    cijene = re.findall(r"\d[\d.,]*\s*(?:€|eur)", tekst or "", re.I)
    return broj(cijene[-1]) if cijene else broj(tekst)


def sobe_iz_naslova(naslov):
    for uzorak, n in SOBE:
        if uzorak.search(naslov):
            return n
    return None


def bez_kvacica(t):
    return t.lower().translate(str.maketrans("čćžšđ", "cczsd"))


def kvart_iz_teksta(tekst):
    n = bez_kvacica(tekst)
    for k in sorted(KVARTOVI, key=len, reverse=True):
        # "Stanovi" je i obična riječ, pa se prepoznaje samo uz "Zadar"
        uzorak = r"zadar\W+stanovi\b" if k == "Stanovi" else r"\b" + re.escape(bez_kvacica(k)) + r"\b"
        if re.search(uzorak, n):
            return k
    return ""


def ocisti(t):
    return re.sub(r"\s+", " ", t or "").strip()


def tekst(el, selektor=None):
    if el is not None and selektor:
        el = el.select_one(selektor)
    return ocisti(el.get_text(" ")) if el is not None else ""


def juha(html):
    return BeautifulSoup(html, "html.parser")


# --- predlošci agencijskih stranica -------------------------------------------
# Svaki parser vraća rječnike s poljima naslov, url, cijena, povrsina, lokacija
# i po želji grad, kvart, sobe, novogradnja (True/None), najam, stan, oznake.

def parsiraj_artis(html, baza):
    for box in juha(html).select(".property-box"):
        a = box.select_one(".title a")
        if not a:
            continue
        yield {
            "naslov": tekst(a),
            "url": urljoin(baza, a["href"]),
            "cijena": broj(tekst(box, ".price-box")),
            "povrsina": broj(tekst(box, ".footer .col-5")),
            "lokacija": tekst(box, ".location"),
            "stan": bool(re.search(r"stan|flat", tekst(box, ".footer .text-right"), re.I)),
        }


def parsiraj_toplokacija(html, baza):
    for item in juha(html).select(".at-property-item"):
        a = item.select_one(".at-property-location h4 a")
        if not a:
            continue
        lis = item.select(".at-property-dis li")
        yield {
            "naslov": tekst(a),
            "url": urljoin(baza, a["href"]),
            "cijena": broj(tekst(item, ".at-property-img h4")),
            "povrsina": broj(tekst(lis[0])) if lis else None,
            "lokacija": tekst(item, ".at-property-location p"),
            "stan": bool(re.search(r"stan|flat", tekst(lis[1]), re.I)) if len(lis) > 1 else True,
            "najam": bool(re.search(r"najam|rent", " ".join(tekst(h) for h in item.select(".at-property-img h5")), re.I)),
        }


def parsiraj_mareta(html, baza):
    """Platforma 100m2 (Next.js); kartice imaju data-qa oznake."""
    for a in juha(html).select("a[data-qa=listing-card]"):
        href = a["href"]
        mjesto = href.rsplit("-zupanija-", 1)[-1] if "-zupanija-" in href else ""
        yield {
            "naslov": tekst(a, "[data-qa=listing-title]"),
            "url": urljoin(baza, href),
            "cijena": zadnja_cijena(tekst(a, "[data-qa=listing-price-badge]")),
            "povrsina": broj(tekst(a, "[data-qa=listing-size]")),
            "lokacija": "",
            "grad": mjesto.replace("-", " ").title(),
            "stan": "-stan-" in href,
        }


def parsiraj_emporia(html, baza):
    """WordPress s WPSight karticama; godina gradnje i oznaka novogradnje su u popisu."""
    for w in juha(html).select(".listing-wrap"):
        d = w.select_one(".listing")
        a = w.select_one(".entry-title a")
        if not d or not a:
            continue
        det = {tekst(x, ".listing-details-label"): tekst(x, ".listing-details-value") for x in w.select(".listing-details-detail")}
        cijena = w.select_one("[itemprop=price]")
        naslov = tekst(a)
        godina = broj(det.get("Godina građenja", ""))
        yield {
            "naslov": naslov,
            "url": a["href"],
            "cijena": broj(cijena.get("content")) if cijena else None,
            "povrsina": broj(det.get("Stambeni prostor", "")),
            "lokacija": naslov.split("–", 1)[1] if "–" in naslov else "",
            "sobe": broj(det.get("Spavaće sobe", "")),
            "novogradnja": True if ("feature-novogradnja" in d.get("class", []) or (godina and godina >= OVA_GODINA - 1)) else None,
        }


def parsiraj_demare(html, baza):
    for a in juha(html).select("a.prop-box2-link"):
        naslov = tekst(a, ".propbox2-title")
        info = a.select(".propbox2-basicinfo div")
        yield {
            "naslov": naslov,
            "url": urljoin(baza, a["href"]),
            "cijena": broj(tekst(a, ".propbox2-price")),
            "povrsina": broj(tekst(info[0])) if info else None,
            "lokacija": naslov.replace(" - ", ", ", 1),
            "opis": tekst(a, ".prop-decsription"),
        }


def detalj_demare(html):
    t = tekst(juha(html))
    stanje = re.search(r"Stanje\s+(.+?)\s+(?:Dodatni|Grijanje|Opis|Namještaj)", t)
    godina = re.search(r"Godina izgradnje\s+(\d{4})", t)
    sobe = re.search(r"Broj soba\s+(\d+)", t)
    novo = bool(stanje and NOVOGRADNJA.search(stanje.group(1))) or bool(godina and int(godina.group(1)) >= OVA_GODINA - 1)
    return {"novogradnja": novo, "sobe": int(sobe.group(1)) if sobe else None}


def parsiraj_ninadom(html, baza):
    for c in juha(html).select(".box-item.list-item"):
        a = c.select_one("h2.title a")
        if not a:
            continue
        info = {i.get("title"): tekst(i) for i in c.select(".info")}
        yield {
            "naslov": tekst(a),
            "url": a["href"],
            "cijena": broj(tekst(c, ".price")),
            "povrsina": broj(info.get("Total area", "")),
            "lokacija": "",
            "grad": "Zadar" if re.search(r"zadar", tekst(a) + a["href"], re.I) else "",
        }


def detalj_ninadom(html):
    t = tekst(juha(html))
    stanje = re.search(r"Condition:\s*(.+?)\s+(?:Energy|Distance|Description|Heating)", t)
    kvart = re.search(r"Quarter:\s*(.+?)\s+(?:Price/m|Total area|Rooms)", t)
    sobe = re.search(r"Rooms:\s*(\d+)", t)
    return {
        "novogradnja": bool(stanje and NOVOGRADNJA.search(stanje.group(1))),
        "kvart": ocisti(kvart.group(1)) if kvart else "",
        "sobe": int(sobe.group(1)) if sobe else None,
    }


def parsiraj_citadela(html, baza):
    for g in juha(html).select(".property_grid"):
        a = g.select_one(".property-text a")
        if not a:
            continue
        povrsina = next((tekst(li) for li in g.select(".quantity li") if "Površina" in tekst(li)), "")
        yield {
            "naslov": tekst(a),
            "url": a["href"],
            "cijena": broj(tekst(g, ".sale_amount")),
            "povrsina": broj(povrsina.replace("Površina", "")),
            "lokacija": tekst(g, ".property-text span"),
            "najam": "type-rent" in str(g.select_one(".featured_btn")),
        }


def parsiraj_nelmar(html, baza):
    """WordPress tema Houzez; popis je već kategorija novogradnje."""
    for c in juha(html).select(".item-listing-wrap"):
        a = c.select_one(".item-title a")
        if not a:
            continue
        naslov = tekst(a)
        adresa = [d.strip() for d in tekst(c, ".item-address").split(",")]
        yield {
            "naslov": naslov,
            "url": a["href"],
            "cijena": broj(tekst(c, ".item-price")),
            "povrsina": broj(tekst(c, ".item-amenities .h-area")),
            "lokacija": ", ".join(([adresa[-1]] + adresa[:-1]) if adresa else []),
            "sobe": broj(tekst(c, ".item-amenities .h-beds")),
            "stan": bool(re.search(r"\bstan|penthouse|apartman", naslov, re.I)) and not NIJE_STAN.search(naslov),
        }


def stranica_s_parametrom(osnova, oblik):
    return lambda n: osnova if n == 1 else osnova + oblik.format(n=n)


AGENCIJE = [
    {
        "id": "artis",
        "naziv": "Artis nekretnine",
        "web": "https://www.artis-nekretnine.hr",
        "stranica": stranica_s_parametrom("https://www.artis-nekretnine.hr/stanovi-zadar-novogradnja", "?page={n}"),
        "parser": parsiraj_artis,
        "kategorija_novogradnja": True,
    },
    {
        "id": "toplokacija",
        "naziv": "Top Lokacija nekretnine",
        "web": "https://www.toplokacija-nekretnine.com",
        "stranica": stranica_s_parametrom("https://www.toplokacija-nekretnine.com/stanovi", "?page={n}"),
        "parser": parsiraj_toplokacija,
    },
    {
        "id": "mareta",
        "naziv": "Mareta nekretnine",
        "web": "https://mareta-nekretnine.hr",
        # popis stanova stane na jednu stranicu, a parametar ?page se zanemaruje
        "stranica": lambda n: "https://mareta-nekretnine.hr/nekretnine/stan" if n == 1 else None,
        "parser": parsiraj_mareta,
    },
    {
        "id": "emporia",
        "naziv": "Emporia nekretnine",
        "web": "https://www.emporia-nekretnine.com",
        "stranica": stranica_s_parametrom("https://www.emporia-nekretnine.com/nekretnine/stanovi/", "page/{n}/"),
        "parser": parsiraj_emporia,
    },
    {
        "id": "demare",
        "naziv": "Demare nekretnine",
        "web": "https://www.demare.hr",
        "stranica": lambda n: "https://www.demare.hr/nekretnine/stanovi.html" + (f"?start={(n - 1) * 12}" if n > 1 else ""),
        "parser": parsiraj_demare,
        "detalj": detalj_demare,
    },
    {
        "id": "ninadom",
        "naziv": "Nina dom nekretnine",
        "web": "https://www.nina-dom.hr",
        "stranica": lambda n: "https://www.nina-dom.hr/realestates/action-1/type-2" if n == 1 else f"https://www.nina-dom.hr/flats-for-sale/page-{n}",
        "parser": parsiraj_ninadom,
        "detalj": detalj_ninadom,
    },
    {
        "id": "citadela",
        "naziv": "Citadela nekretnine",
        "web": "https://www.citadela.hr",
        "stranica": stranica_s_parametrom("https://www.citadela.hr/hr/stanovi", "?page={n}"),
        "parser": parsiraj_citadela,
    },
    {
        "id": "nelmar",
        "naziv": "Nelmar nekretnine",
        "web": "https://nelmar-nekretnine.com",
        "stranica": stranica_s_parametrom("https://nelmar-nekretnine.com/vrsta-nekretnine/prodaja-nekretnina-u-novogradnji-u-zadru/", "page/{n}/"),
        "parser": parsiraj_nelmar,
        "kategorija_novogradnja": True,
    },
]


def preuzmi(sesija, url):
    r = sesija.get(url, timeout=30)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = "utf-8"
    return r.text


def stranice(sesija, agencija):
    """Prolazi kroz stranice popisa dok ne prestanu donositi nove oglase."""
    vidjeni = set()
    for n in range(1, MAX_STRANICA + 1):
        url = agencija["stranica"](n)
        if not url:
            return
        if n > 1:
            time.sleep(PAUZA)
        try:
            html = preuzmi(sesija, url)
        except requests.HTTPError as e:
            if n > 1 and e.response is not None and e.response.status_code == 404:
                return
            raise
        novi = [o for o in agencija["parser"](html, agencija["web"]) if o["url"] not in vidjeni]
        if not novi:
            return
        for o in novi:
            vidjeni.add(o["url"])
            yield o


def lokacija(o):
    """Vraća (grad, kvart) iz polja lokacija poput 'Zadar, Diklo' i naslova."""
    dijelovi = [d.strip() for d in re.split(r",", o.get("lokacija") or "") if d.strip()]
    grad = o.get("grad") or (dijelovi[0] if dijelovi else "")
    if not grad and re.search(r"\bzadar\b", o["naslov"], re.I):
        grad = "Zadar"
    kvart = o.get("kvart") or next((d for d in dijelovi if d.lower() != "zadar"), "") if grad.lower() == "zadar" else ""
    if kvart.lower() == "zadar":
        kvart = ""
    if not kvart:
        kvart = kvart_iz_teksta(o["naslov"])
    return grad, kvart


def kandidat(o, grad):
    """Grubi filtar prije čitanja stranice oglasa: stan na prodaju u gradu Zadru."""
    return (o.get("stan", True) and not o.get("najam") and not NAJAM.search(o["naslov"])
            and grad.lower() == "zadar" and not OKOLICA.search(o["naslov"]))


def normaliziraj(o, agencija, sada):
    grad, kvart = lokacija(o)
    tekst_oglasa = o["naslov"] + " " + " ".join(o.get("oznake", []))
    cijena = o["cijena"] if isinstance(o["cijena"], (int, float)) and o["cijena"] > 1000 else None
    povrsina = o["povrsina"] if isinstance(o["povrsina"], (int, float)) and o["povrsina"] > 10 else None
    sobe = sobe_iz_naslova(o["naslov"])
    if sobe is None and isinstance(o.get("sobe"), int) and 0 < o["sobe"] < 10:
        sobe = o["sobe"]
    return {
        "id": agencija["id"] + ":" + o["url"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".html"),
        "naslov": o["naslov"],
        "url": o["url"],
        "agencija": agencija["naziv"],
        "agencija_web": agencija["web"],
        "grad": grad,
        "kvart": kvart,
        "cijena": cijena,
        "povrsina": povrsina,
        "cijena_m2": round(cijena / povrsina) if cijena and povrsina else None,
        "sobe": sobe,
        "projekt": povrsina is None and bool(re.search(r"kompleks|projekt|zgrad|development|apartments in|stanovi u", o["naslov"], re.I)),
        "novogradnja": bool(agencija.get("kategorija_novogradnja") or o.get("novogradnja") or NOVOGRADNJA.search(tekst_oglasa)),
        "prvi_put_vidjen": sada,
        "zadnji_put_vidjen": sada,
    }


def ucitaj_json(put, zadano):
    try:
        return json.loads(Path(put).read_text("utf-8"))
    except (OSError, ValueError):
        return zadano


def skupi_agenciju(sesija, ag, sada, prirucna, uzorci):
    if uzorci:
        html = (Path(uzorci) / f"{ag['id']}.html").read_text("utf-8")
        sirovi = list(ag["parser"](html, ag["web"]))
    else:
        sirovi = list(stranice(sesija, ag))

    zadrzani, novih_detalja = [], 0
    for o in sirovi:
        grad, _ = lokacija(o)
        if not kandidat(o, grad):
            continue
        if ag.get("detalj") and not o.get("novogradnja") and not NOVOGRADNJA.search(o["naslov"]):
            # novogradnja se vidi tek na stranici oglasa; rezultat se pamti da se ne čita svaki dan
            det = prirucna.get(o["url"])
            if det is None and not uzorci and novih_detalja < MAX_DETALJA:
                time.sleep(PAUZA)
                try:
                    det = ag["detalj"](preuzmi(sesija, o["url"]))
                    prirucna[o["url"]] = det
                    novih_detalja += 1
                except requests.RequestException as e:
                    print(f"  {o['url']}: {e}", file=sys.stderr)
            if not det:
                continue
            o.update({k: v for k, v in det.items() if v})
        n = normaliziraj(o, ag, sada)
        if n["novogradnja"]:
            zadrzani.append(n)
    return sirovi, zadrzani


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uzorci", help="mapa sa spremljenim HTML-om umjesto preuzimanja")
    ap.add_argument("--izlaz", default=str(IZLAZ))
    ap.add_argument("--samo", help="pokreni samo agencije s ovim id-jevima, odvojene zarezom")
    args = ap.parse_args()

    sada = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    izlaz = Path(args.izlaz)
    stari = {o["id"]: o for o in ucitaj_json(izlaz, {}).get("oglasi", [])}
    prirucna = ucitaj_json(PRIRUCNA, {})

    sesija = requests.Session()
    sesija.headers["User-Agent"] = UA
    agencije = [a for a in AGENCIJE if not args.samo or a["id"] in args.samo.split(",")]
    oglasi, izvori, vidjeni_url = [], [], set()
    for ag in agencije:
        try:
            sirovi, zadrzani = skupi_agenciju(sesija, ag, sada, prirucna, args.uzorci)
            for o in zadrzani:
                if o["id"] in stari:
                    o["prvi_put_vidjen"] = stari[o["id"]].get("prvi_put_vidjen", sada)
            zadrzani = [o for o in zadrzani if o["url"] not in vidjeni_url]
            vidjeni_url.update(o["url"] for o in zadrzani)
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
    if not args.uzorci:
        PRIRUCNA.write_text(json.dumps(prirucna, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
    print(f"Spremljeno {len(oglasi)} oglasa u {izlaz}")


if __name__ == "__main__":
    main()

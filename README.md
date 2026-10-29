# Novogradnja Zadar

Pregled novih stanova za prodaju u Zadru, skupljenih s web stranica zadarskih agencija za nekretnine.

Stranica prikazuje samo osnovne podatke (naslov, kvart, cijena, površina, broj soba) i poveznicu na izvorni oglas kod agencije. Opisi i fotografije se ne kopiraju.

## Kako radi

- `skupljac/skupi.py` preuzima popise oglasa s agencijskih stranica, zadržava novogradnju u Zadru i sprema rezultat u `podaci/oglasi.json`.
- GitHub Actions (`.github/workflows/skupi-oglase.yml`) pokreće skupljač svaki dan u 5:00 UTC i sprema promjene. Može se pokrenuti i ručno na kartici Actions.
- `index.html` je statična stranica koja učitava `podaci/oglasi.json`. Objavljuje se preko GitHub Pagesa.

## Dodavanje agencije

U `skupljac/skupi.py` dodajte zapis u popis `AGENCIJE`. Ako agencija koristi jedan od postojećih predložaka stranice (`parsiraj_property_box`, `parsiraj_at_property`), dovoljno je navesti URL popisa stanova. Inače treba napisati novu funkciju za čitanje kartica oglasa.

## Lokalno pokretanje

```
pip install -r skupljac/requirements.txt
python skupljac/skupi.py
python -m http.server
```

Zatim otvorite http://localhost:8000.

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor Pozwolen na Budowe (RWDZ / GUNB) - wersja CSV/ZIP, strumieniowa.
Radzi sobie z duzymi plikami (150 MB+), bo czyta je linia po linii.
Przeznaczony do uruchamiania w chmurze na GitHub Actions (co tydzien).

Zrodlo (link do pliku wojewodztwa, ZIP lub CSV) podaj przez:
  - zmienna srodowiskowa SOURCE_URL  (zalecane na GitHub Actions), albo
  - argument --source <url-lub-sciezka>
"""

import argparse, csv, hashlib, io, json, os, smtplib, sys, tempfile
import urllib.request, zipfile
from datetime import datetime
from email.message import EmailMessage

# ---- KONFIGURACJA (mozesz edytowac) ----
DATA_URL = os.environ.get("SOURCE_URL", "")
KEYWORDS = ["magazyn", "centrum logistyczn", "park logistyczn",
            "centrum dystrybucyjn", "logistyczn"]
EXCLUDE  = ["magazyn energii", "magazynu energii",
            "magazynow energii", "magazynów energii", "magazyny energii"]
# ----------------------------------------

csv.field_size_limit(10**7)


def log(m): print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def pobierz(source):
    """URL -> plik tymczasowy; lokalna sciezka -> bez zmian."""
    if not source.lower().startswith("http"):
        return source
    log(f"Pobieram: {source}")
    tmp = tempfile.NamedTemporaryFile(delete=False)
    req = urllib.request.Request(source, headers={"User-Agent": "MonitorPozwolen/2.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp.name, "wb") as out:
        while True:
            chunk = r.read(1 << 16)
            if not chunk: break
            out.write(chunk)
    log(f"Pobrano {os.path.getsize(tmp.name)/1e6:.1f} MB")
    return tmp.name


def otworz_csv(sciezka, encoding):
    """Zwraca (strumien_tekstowy, uchwyt_zip_lub_None). Czyta strumieniowo."""
    if zipfile.is_zipfile(sciezka):
        zf = zipfile.ZipFile(sciezka)
        csvy = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csvy:
            raise SystemExit("W ZIP nie ma pliku .csv")
        log(f"ZIP -> czytam {csvy[0]}")
        return io.TextIOWrapper(zf.open(csvy[0]), encoding=encoding, errors="replace"), zf
    return open(sciezka, encoding=encoding, errors="replace"), None


def pasuje(t, lista): return any(k in t for k in lista)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", default=DATA_URL)
    p.add_argument("--encoding", default="utf-8", help="utf-8 lub cp1250")
    p.add_argument("--wojewodztwo", default="")
    p.add_argument("--seen", default="widziane.json")
    p.add_argument("--outdir", default="wyniki")
    p.add_argument("--inspect", action="store_true")
    a = p.parse_args()
    if not a.source:
        raise SystemExit("Brak zrodla. Ustaw SOURCE_URL albo --source.")

    sciezka = pobierz(a.source)
    stream, zf = otworz_csv(sciezka, a.encoding)
    try:
        pierwsza = stream.readline()
        delim = ";" if pierwsza.count(";") >= pierwsza.count(",") else ","
        naglowek = next(csv.reader([pierwsza], delimiter=delim))

        if a.inspect:
            log(f"Rozdzielnik: '{delim}'  Kolumn: {len(naglowek)}")
            print("Naglowek:", naglowek)
            for i, row in enumerate(csv.reader(stream, delimiter=delim)):
                print("Wiersz:", row); 
                if i >= 2: break
            return

        idx_numer = next((i for i, c in enumerate(naglowek)
                          if "numer" in c.lower()), -1)
        keywords = [k.casefold() for k in KEYWORDS]
        exclude  = [e.casefold() for e in EXCLUDE]
        woj = a.wojewodztwo.casefold()

        widziane = set(json.load(open(a.seen, encoding="utf-8"))) \
                   if os.path.exists(a.seen) else set()

        log("Przeszukuje...")
        nowe, n = [], 0
        for row in csv.reader(stream, delimiter=delim):
            n += 1
            pelny = " ".join(row)
            low = pelny.casefold()
            if woj and woj not in low: continue
            if pasuje(low, exclude): continue
            if not pasuje(low, keywords): continue
            rid = row[idx_numer] if (0 <= idx_numer < len(row) and row[idx_numer]) \
                  else hashlib.sha1(pelny.encode()).hexdigest()
            if rid in widziane: continue
            widziane.add(rid)
            rec = dict(zip(naglowek, row)); rec["_id"] = rid
            nowe.append(rec)
        log(f"Przejrzano {n} wierszy. Nowych: {len(nowe)}")
    finally:
        stream.close()
        if zf: zf.close()

    if nowe:
        os.makedirs(a.outdir, exist_ok=True)
        plik = os.path.join(a.outdir, f"nowe_{datetime.now():%Y%m%d_%H%M}.csv")
        kol = sorted({k for r in nowe for k in r})
        with open(plik, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=kol); w.writeheader(); w.writerows(nowe)
        log(f"Zapisano: {plik}")
        for r in nowe[:15]:
            print("  - " + " | ".join(f"{k}={v}" for k, v in r.items()
                                      if k != "_id" and v)[:160])
        wyslij_mail(nowe)
    json.dump(sorted(widziane), open(a.seen, "w", encoding="utf-8"), ensure_ascii=False)


def wyslij_mail(nowe):
    h, u, pw, do = (os.environ.get(x) for x in
                    ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not all([h, u, pw, do]): return
    body = [f"Nowych wpisow: {len(nowe)}\n"]
    for r in nowe:
        body.append("- " + " | ".join(f"{k}: {v}" for k, v in r.items()
                                      if k != "_id" and v)[:400])
    m = EmailMessage()
    m["Subject"] = f"Monitor pozwolen: {len(nowe)} nowych"
    m["From"], m["To"] = u, do
    m.set_content("\n".join(body))
    with smtplib.SMTP_SSL(h, int(os.environ.get("SMTP_PORT", "465"))) as s:
        s.login(u, pw); s.send_message(m)
    log(f"Wyslano e-mail na {do}")


if __name__ == "__main__":
    main()

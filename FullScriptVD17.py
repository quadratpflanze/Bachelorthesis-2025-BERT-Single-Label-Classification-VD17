import os
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from zipfile import ZipFile
from lxml import etree
import csv
import re
from tqdm import tqdm
from collections import defaultdict
import time

# Konfiguration (absolute Pfade müssen durch eigene Pfade ersetzt werden)
OAI_BASE_URL = "https://oai.sbb.berlin/?"
OAI_SET = "17.Jahrhundert"
MAX_DOWNLOADS = 3000
MAX_PER_GATTUNG = 300
GATTUNGSBEGRIFFE_FILE = "/home/lena/Downloads/Gattungsbegriffe0-272.txt"

# Pfade
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ZIP_DIR = os.path.join(BASE_DIR, "ocr_zips1651-1700")
UNZIP_DIR = os.path.join(BASE_DIR, "ocr_unzipped1651-1700")
TEXT_DIR = os.path.join(BASE_DIR, "ocr_texts1651-1700")
CSV_PATH = os.path.join(BASE_DIR, "ocr_metadata1651-1700.csv")
TITLE_LIST_PATH = os.path.join(BASE_DIR, "ocr_titles1651-1700.txt")
REJECTED_LIST_PATH = os.path.join(BASE_DIR, "rejected_identifiers1651-1700.txt")

os.makedirs(ZIP_DIR, exist_ok=True)
os.makedirs(UNZIP_DIR, exist_ok=True)
os.makedirs(TEXT_DIR, exist_ok=True)

# Lade Gattungsbegriffe als Dictionary
GATTUNGEN_INDEX = {}
with open(GATTUNGSBEGRIFFE_FILE, "r", encoding="utf-8") as f:
    for line in f:
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            nummer, begriff = parts
            GATTUNGEN_INDEX[begriff.strip().lstrip("= ").lower()] = int(nummer)


# weiche Anfragen, um Server nicht zu überlasten
def safe_request(url, max_retries=3, delay=5):
    for attempt in range(max_retries):
        try:
            return requests.get(url, timeout=(1200, 1800))  # 120sek für Serverantwort, 180 sek um Datei zu lesen
        except requests.exceptions.RequestException as e:
            print(f"✗ Fehler beim Abruf (Versuch {attempt + 1}/{max_retries}): {e}")
            time.sleep(delay)
    return None


def list_identifiers():
    identifiers = []
    params = {
        "verb": "ListIdentifiers",
        "metadataPrefix": "oai_dc",
        "set": OAI_SET
    }
    while True:
        url = OAI_BASE_URL + urlencode(params)
        resp = safe_request(url)
        if resp is None or resp.status_code != 200:
            break
        root = ET.fromstring(resp.content)
        ns = {"oai": "http://www.openarchives.org/OAI/2.0/"}
        for identifier in root.findall(".//oai:identifier", ns):
            identifiers.append(identifier.text)
        # if len(identifiers) >= MAX_DOWNLOADS * 10:  # Sicherheitsgrenze
        #    return identifiers
        token = root.find(".//oai:resumptionToken", ns)
        if token is None or not token.text:
            break
        params = {
            "verb": "ListIdentifiers",
            "resumptionToken": token.text
        }
    return identifiers


def extract_ppn(oai_identifier):
    return oai_identifier.split(":")[-1].replace("PPN", "")

#Entfernt oder ersetzt problematische Zeichen im OCR-Fließtext, damit CSV korrekt mit 2 Spalten funktioniert.
def sanitize_for_csv(text):
    text = text.replace('"', '»')  # ersetzt Anführungszeichen
    text = text.replace('\n', ' ')  # entfernt Zeilenumbrüche
    text = text.replace('\r', '')  # entfernt Windows-Zeilenumbruch
    return text.strip()


# Download und speichern der PPN Zip-Dateien
def download_and_unzip_ocr(ppn):
    url = f"https://content.staatsbibliothek-berlin.de/dc/{ppn}.ocr.zip"

    max_retries = 3
    delay = 10
    zip_path = os.path.join(ZIP_DIR, f"{ppn}.ocr.zip")

    for attempt in range(max_retries):
        try:
            response = requests.get(url, stream=True, allow_redirects=False, timeout=(30, 300))
            if response.status_code == 200 and "zip" in response.headers.get("Content-Type", "").lower():
                with open(zip_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                print(f"✓ OCR-ZIP gespeichert für {ppn}")
                break
            else:
                print(f"✗ Keine gültige ZIP-Datei für {ppn} (Status {response.status_code})")
                return None
        except requests.RequestException as e:
            print(f"✗ Fehler beim Download (Versuch {attempt + 1}/{max_retries}) bei {ppn}: {e}")
            time.sleep(delay)
    else:
        print(f"✗ Abbruch nach {max_retries} Versuchen für {ppn}")
        return None

    # Entpacken der ZIP-Dateien
    unzip_target = os.path.join(UNZIP_DIR, f"PPN{ppn}")
    os.makedirs(unzip_target, exist_ok=True)
    try:
        with ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(unzip_target)
        print(f"✓ Entpackt nach: {unzip_target}")
        return unzip_target
    except Exception as e:
        print(f"✗ Fehler beim Entpacken von {ppn}: {e}")
        return None


# Extrahiert Volltext aus XML-Datei
def extract_text_from_xml(file_path):
    try:
        with open(file_path, 'rb') as f:
            tree = etree.parse(f)
        words = tree.xpath('//@CONTENT')
        return ' '.join(words)
    except Exception as e:
        print(f"✗ Fehler beim Parsen von {file_path}: {e}")
        return ""


# Zusammenführung der extrahierten Texte als Volltext (.txt)
def process_unzipped_folder(ppn, unzip_path):
    all_text = []
    for file in sorted(os.listdir(unzip_path)):
        if file.endswith(".xml"):
            xml_path = os.path.join(unzip_path, file)
            text = extract_text_from_xml(xml_path)
            all_text.append(text)
    full_text = '\n'.join(all_text)
    if full_text:
        output_path = os.path.join(TEXT_DIR, f"PPN{ppn}.txt")
        with open(output_path, 'w', encoding='utf-8') as out:
            out.write(full_text)
        print(f"✓ OCR-Text gespeichert: {output_path}")
    return full_text


# Auslesen der Gattungsbegriffe aus den Metadaten (wichtig: nur mods:genre definiert die VD Gattungsbegriffe!)
def get_mods_genres(ppn):
    url = f"https://content.staatsbibliothek-berlin.de/dc/PPN{ppn}.mets.xml"
    try:
        resp = safe_request(url)
        if resp is None or resp.status_code != 200:
            print(f"✗ Konnte METS/MODS für {ppn} nicht laden")
            return None, "", "", ""
        tree = etree.fromstring(resp.content)
        ns = {"mods": "http://www.loc.gov/mods/v3"}
        title_elem = tree.find(".//mods:title", namespaces=ns)
        creator_elem = tree.find(".//mods:namePart", namespaces=ns)
        date_elem = tree.find(".//mods:dateIssued", namespaces=ns)
        title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
        creator = creator_elem.text.strip() if creator_elem is not None and creator_elem.text else ""
        date = date_elem.text.strip() if date_elem is not None and date_elem.text else ""
        matched_indexes = []
        print("  ─ Genre-Felder:")
        genres = tree.findall(".//mods:genre", namespaces=ns)
        for genre in genres:
            raw = genre.text.strip() if genre.text else ""
            raw_clean = raw.lower()
            print(f"    • {raw} → {raw_clean}")
            if raw_clean in GATTUNGEN_INDEX:
                matched_indexes.append(str(GATTUNGEN_INDEX[raw_clean]))
            else:
                for wort, index in GATTUNGEN_INDEX.items():
                    if wort in raw_clean:
                        matched_indexes.append(str(index))
        if '53' in matched_indexes and len(matched_indexes) > 1:
            matched_indexes.remove(
                '53')  # Schließt den Gattungsbegriff "Einblattdruck" aus, wenn es nicht der einzige hinterlegte ist)
        if not matched_indexes:
            return None, title, creator, date
        matched = [sorted(matched_indexes, key=lambda x: int(x))[-1]]
        return matched, title, creator, date
    except Exception as e:
        print(f"✗ Fehler beim Verarbeiten von METS/MODS für {ppn}: {e}")
        return None, "", "", ""


# Hauptfunktion: Schreibt heruntergeladene Texte und ihre dazugehörigen Gattungsbegriffe in ein CSV-file
def main():
    all_identifiers = list_identifiers()
    print(f"{len(all_identifiers)} Identifier gefunden. Ziel: {MAX_DOWNLOADS} OCR-Downloads.")
    downloaded = 0
    written = 0
    gattung_counter = defaultdict(int)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as csvfile, \
            open(TITLE_LIST_PATH, "w", encoding="utf-8") as titlefile, \
            open(REJECTED_LIST_PATH, "w", encoding="utf-8") as rejectfile:
        writer = csv.writer(csvfile,
                            quoting=csv.QUOTE_ALL)  # wichtig, da fliesstexte kommata enthalten und die csv spalten kaputt machen
        for identifier in tqdm(all_identifiers, desc="Verarbeitung"):
            if downloaded >= MAX_DOWNLOADS:
                break
            ppn = extract_ppn(identifier)
            print(f"\nVerarbeite: PPN{ppn}")
            gattung_ids, title, creator, date = get_mods_genres(ppn)
            if gattung_ids is None:
                print("→ Übersprungen: Kein gültiger Gattungsbegriff gefunden")
                rejectfile.write(f"{ppn} | Kein gültiger Gattungsbegriff\n")
                continue
            year_match = re.search(r"(16\d{2}|17\d{2})", date)
            year = int(year_match.group(1)) if year_match else None
            if not (year and 1651 <= year <= 1700):
                print(f"→ Übersprungen wegen Erscheinungsjahr {date}")
                rejectfile.write(f"{ppn} | Erscheinungsjahr {date}\n")
                continue
            if gattung_counter[gattung_ids[0]] >= MAX_PER_GATTUNG:
                print(f"→ Übersprungen: Gattungsgrenze erreicht für {gattung_ids[0]}")
                rejectfile.write(f"{ppn} | Gattungsgrenze {gattung_ids[0]} erreicht\n")
                continue
            unzip_path = download_and_unzip_ocr(ppn)
            if unzip_path:
                text = process_unzipped_folder(ppn, unzip_path)
                if not text.strip():
                    rejectfile.write(f"{ppn} | Leerer Text\n")
                    continue
                titlefile.write(f"PPN{ppn} | {gattung_ids} | {title} | {creator} | {date}\n")
                print(f"→ Gattungsnummern für PPN{ppn}: {gattung_ids}")
                clean_text = sanitize_for_csv(text)
                writer.writerow([clean_text, ";".join(gattung_ids)])
                print(
                    f"✓ Geschrieben in CSV: Volltextlänge = {len(text.strip())} Zeichen | Gattungen: {';'.join(gattung_ids)}")
                gattung_counter[gattung_ids[0]] += 1
                downloaded += 1
                written += 1
    print(f"\n✓ Fertig: {downloaded} OCR-Texte extrahiert und gespeichert.")
    print(f"✓ CSV enthält {written} Zeilen.")


if __name__ == "__main__":
    main()
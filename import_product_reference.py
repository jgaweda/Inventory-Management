#!/usr/bin/env python3
"""
One-time import script for populating the product_reference table from an
Excel (.xlsx) or tab-separated (.tsv) spreadsheet.

Expected columns (first row is header):
    Codename | Model Name | Wi-Fi Gen | Year | Wireless Chip Set Manufacturer | Wireless Chipset Codename | FW Codebase

Usage:
    python import_product_reference.py <file.xlsx>
    python import_product_reference.py <file.tsv>

Optional flags:
    --print-technology <Ink|Laser>   Set print technology for all imported rows
    --dry-run                        Show what would be imported without writing
    --sheet <name>                   Excel sheet name (default: first sheet)
"""

import argparse
import csv
import sys
import os

# Ensure the project root is on the path so we can import database.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database as db


# Map spreadsheet header names to database column names
HEADER_MAP = {
    'codename': 'codename',
    'model name': 'model_name',
    'wi-fi gen': 'wifi_gen',
    'wifi gen': 'wifi_gen',
    'year': 'year',
    'wireless chip set manufacturer': 'chip_manufacturer',
    'wireless chipset manufacturer': 'chip_manufacturer',
    'chip manufacturer': 'chip_manufacturer',
    'wireless chipset codename': 'chip_codename',
    'chip codename': 'chip_codename',
    'fw codebase': 'fw_codebase',
    'print technology': 'print_technology',
    'cartridge/toner': 'cartridge_toner',
    'cartridge_toner': 'cartridge_toner',
    'cartridge': 'cartridge_toner',
    'toner': 'cartridge_toner',
    'variant': 'variant',
}


def normalize_header(h):
    """Lowercase and strip a header for matching."""
    return str(h).strip().lower()


def parse_xlsx(filepath, sheet_name=None):
    """Read an Excel file and yield dicts with normalized keys."""
    try:
        import openpyxl
    except ImportError:
        print("Error: openpyxl is required for .xlsx files.")
        print("Install it with:  pip install openpyxl")
        sys.exit(1)

    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
    print(f"  Sheet: {ws.title}")

    rows = ws.iter_rows()
    raw_headers = [cell.value or '' for cell in next(rows)]
    headers = []
    for h in raw_headers:
        key = HEADER_MAP.get(normalize_header(h))
        if key is None and str(h).strip():
            print(f"  Warning: unknown column '{str(h).strip()}' — skipping")
        headers.append(key)

    for row_num, row in enumerate(rows, start=2):
        values = [cell.value for cell in row]
        if not any(v is not None and str(v).strip() for v in values):
            continue  # skip blank rows
        record = {}
        for i, val in enumerate(values):
            if i < len(headers) and headers[i]:
                record[headers[i]] = str(val).strip() if val is not None else ''
        yield row_num, record

    wb.close()


def parse_tsv(filepath):
    """Read a TSV file and yield dicts with normalized keys."""
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f, delimiter='\t')
        raw_headers = next(reader)
        headers = []
        for h in raw_headers:
            key = HEADER_MAP.get(normalize_header(h))
            if key is None and h.strip():
                print(f"  Warning: unknown column '{h.strip()}' — skipping")
            headers.append(key)

        for row_num, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                continue  # skip blank rows
            record = {}
            for i, val in enumerate(row):
                if i < len(headers) and headers[i]:
                    record[headers[i]] = val.strip()
            yield row_num, record


def main():
    parser = argparse.ArgumentParser(description='Import product references from Excel or TSV')
    parser.add_argument('file', help='Path to the .xlsx or .tsv file')
    parser.add_argument('--print-technology', choices=['Ink', 'Laser', 'Large Format'], default='',
                        help='Set print technology for all imported rows')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview import without writing to database')
    parser.add_argument('--sheet', default=None,
                        help='Excel sheet name (default: first sheet)')
    args = parser.parse_args()

    if not os.path.isfile(args.file):
        print(f"Error: file not found: {args.file}")
        sys.exit(1)

    # Pick parser based on file extension
    ext = os.path.splitext(args.file)[1].lower()
    if ext in ('.xlsx', '.xls'):
        row_iter = parse_xlsx(args.file, args.sheet)
    elif ext in ('.tsv', '.txt', '.csv'):
        row_iter = parse_tsv(args.file)
    else:
        print(f"Error: unsupported file type '{ext}'. Use .xlsx or .tsv")
        sys.exit(1)

    # Initialize database (creates tables if needed)
    db.init_db()

    imported = 0
    skipped = 0

    print(f"Reading: {args.file}")
    print()

    for row_num, record in row_iter:
        codename = record.get('codename', '').strip()
        if not codename:
            print(f"  Row {row_num}: skipped (no codename)")
            skipped += 1
            continue

        # Apply global print_technology if set and not already in the data
        if args.print_technology and not record.get('print_technology'):
            record['print_technology'] = args.print_technology

        if args.dry_run:
            print(f"  Row {row_num}: {codename} — {record.get('model_name', '')} "
                  f"[Wi-Fi {record.get('wifi_gen', '?')}] ({record.get('year', '?')})")
        else:
            db.add_product_reference(
                codename=codename,
                model_name=record.get('model_name', ''),
                wifi_gen=record.get('wifi_gen', ''),
                year=record.get('year', ''),
                chip_manufacturer=record.get('chip_manufacturer', ''),
                chip_codename=record.get('chip_codename', ''),
                fw_codebase=record.get('fw_codebase', ''),
                print_technology=record.get('print_technology', ''),
                cartridge_toner=record.get('cartridge_toner', ''),
            )
        imported += 1

    print()
    if args.dry_run:
        print(f"Dry run complete: {imported} would be imported, {skipped} skipped")
    else:
        print(f"Import complete: {imported} products imported, {skipped} skipped")


if __name__ == '__main__':
    main()

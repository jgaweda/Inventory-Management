"""
Barcode and label utilities for the HP Connectivity Team Inventory System.

Generates QR codes, Code 128 barcodes, individual device labels (600x300 px),
and printable label sheets (US Letter, 3x5 grid).

Dependencies: qrcode, python-barcode, Pillow
"""

import os
import io
import qrcode
from barcode import Code128
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

# Directory where label PNGs are saved
LABELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'labels')


def _ensure_labels_dir():
    """Create the labels directory if it doesn't exist."""
    os.makedirs(LABELS_DIR, exist_ok=True)


def generate_qr_code(data, size=200):
    """
    Generate a QR code image for the given data string.
    Returns a PIL Image resized to size x size pixels.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    return img.resize((size, size), Image.LANCZOS)


def generate_barcode_image(data, width=350, height=80):
    """
    Generate a Code 128 barcode image (no human-readable text below).
    Returns a PIL Image resized to width x height.
    """
    # Render barcode to a bytes buffer
    writer = ImageWriter()
    code = Code128(data, writer=writer)
    buffer = io.BytesIO()
    code.render(writer_options={
        'font_size': 0,
        'text_distance': 0,
        'quiet_zone': 2,
    }).save(buffer, format='PNG')
    buffer.seek(0)

    img = Image.open(buffer).convert('RGB')
    return img.resize((width, height), Image.LANCZOS)


def generate_label(device_id, barcode_value, device_name, save=True):
    """
    Create a 600x300 pixel device label containing:
    - Left: QR code (180x180, centered vertically)
    - Right: Device name, barcode value, team name, Code 128 barcode
    - 1px gray border

    If save=True, writes PNG to static/labels/{device_id}.png.
    Returns the file path (if saved) or the PIL Image.
    """
    label = Image.new('RGB', (600, 300), 'white')
    draw = ImageDraw.Draw(label)

    # Load fonts (use Pillow defaults — no external font files needed)
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 13)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    except (OSError, IOError):
        font_large = ImageFont.load_default()
        font_medium = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # --- Left side: QR code ---
    qr_size = 180
    qr_img = generate_qr_code(barcode_value, size=qr_size)
    qr_y = (300 - qr_size) // 2  # Center vertically
    label.paste(qr_img, (20, qr_y))

    # --- Right side: text and barcode ---
    right_x = 220

    # Device name (bold, truncated at 28 chars)
    display_name = device_name[:28] + '...' if len(device_name) > 28 else device_name
    draw.text((right_x, 30), display_name, fill='black', font=font_large)

    # Barcode value (monospace)
    draw.text((right_x, 65), barcode_value, fill='#333333', font=font_medium)

    # Team name
    draw.text((right_x, 90), 'HP Connectivity Team', fill='#888888', font=font_small)

    # Code 128 barcode image
    try:
        barcode_img = generate_barcode_image(barcode_value, width=340, height=70)
        label.paste(barcode_img, (right_x, 130))
    except Exception:
        # If barcode generation fails, just show text
        draw.text((right_x, 150), barcode_value, fill='black', font=font_medium)

    # 1px gray border around the entire label
    draw.rectangle([0, 0, 599, 299], outline='#cccccc', width=1)

    if save:
        _ensure_labels_dir()
        path = os.path.join(LABELS_DIR, f'{device_id}.png')
        label.save(path, 'PNG')
        return path
    else:
        return label


def generate_label_sheet(devices, cols=3, rows=5):
    """
    Generate a US Letter page (2550x3300 px at 300 DPI) with a grid of labels.
    Each label is 600x300 px. Grid has 75px margins.

    Args:
        devices: list of dicts with device_id, barcode_value, name keys
        cols: number of columns (default 3)
        rows: number of rows (default 5)

    Returns: PIL Image of the full sheet
    """
    page_w, page_h = 2550, 3300
    margin = 75
    label_w, label_h = 600, 300

    # Calculate cell size and spacing
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin
    cell_w = usable_w // cols
    cell_h = usable_h // rows

    sheet = Image.new('RGB', (page_w, page_h), 'white')

    for i, device in enumerate(devices[:cols * rows]):
        col = i % cols
        row = i // cols

        # Generate label (don't save individual file)
        label_img = generate_label(
            device['device_id'],
            device['barcode_value'],
            device['name'],
            save=False,
        )

        # Center label within cell
        cell_x = margin + col * cell_w
        cell_y = margin + row * cell_h
        offset_x = cell_x + (cell_w - label_w) // 2
        offset_y = cell_y + (cell_h - label_h) // 2

        sheet.paste(label_img, (offset_x, offset_y))

    return sheet


def label_exists(device_id):
    """Check if a label PNG already exists for this device."""
    return os.path.isfile(os.path.join(LABELS_DIR, f'{device_id}.png'))


def get_label_path(device_id):
    """Get the file path for a device's label PNG."""
    return os.path.join(LABELS_DIR, f'{device_id}.png')

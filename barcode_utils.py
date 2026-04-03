"""
Barcode and label utilities for the HP Connectivity Team Inventory System.

Generates Code 128 barcodes and individual device labels (1050x450 px = 3.5x1.5"
at 300 DPI) and printable label sheets.

Dependencies: qrcode, python-barcode, Pillow
"""

import os
import io
import qrcode
from barcode import Code128
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont
from runtime_dirs import DATA_DIR

# Directory where label PNGs are saved (writable, outside bundled static)
LABELS_DIR = os.path.join(DATA_DIR, 'static', 'labels')


def _ensure_labels_dir():
    """Create the labels directory if it doesn't exist."""
    os.makedirs(LABELS_DIR, exist_ok=True)


def _find_font(names, size):
    """Try multiple font paths (Linux + macOS) and return the first that works."""
    for name in names:
        for path in [
            f"/usr/share/fonts/truetype/dejavu/{name}",
            f"/usr/share/fonts/truetype/liberation/{name}",
            f"/System/Library/Fonts/{name}",
            f"/Library/Fonts/{name}",
            f"/System/Library/Fonts/Supplemental/{name}",
        ]:
            try:
                return ImageFont.truetype(path, size)
            except (OSError, IOError):
                continue
    # Last resort: try by name only (Pillow searches system paths)
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default(size=size)


BOLD_FONTS = ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
              "Helvetica-Bold.ttf", "Helvetica.ttc", "Arial Bold.ttf"]
MONO_BOLD_FONTS = ["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf",
                   "Courier.ttc", "Menlo.ttc", "Courier New Bold.ttf"]


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
    Uses NEAREST interpolation to keep bars crisp for scanning.
    """
    writer = ImageWriter()
    code = Code128(data, writer=writer)
    buffer = io.BytesIO()
    code.render(writer_options={
        'font_size': 0,
        'text_distance': 0,
        'quiet_zone': 2,
        'module_width': 0.4,
        'module_height': 20,
    }).save(buffer, format='PNG')
    buffer.seek(0)

    img = Image.open(buffer).convert('RGB')
    # Use NEAREST to keep barcode bars sharp (no anti-aliasing blur)
    return img.resize((width, height), Image.NEAREST)


def generate_label(device_id, barcode_value, device_name, save=True):
    """
    Create a 1050x450 pixel device label (3.5x1.5 inches at 300 DPI) containing:
    - Left: QR code (fills height)
    - Right top: Device name (centered above barcode)
    - Right middle: Full-width Code 128 barcode
    - Right bottom: Barcode ID value (centered, large mono font)

    If save=True, writes PNG to static/labels/{device_id}.png.
    Returns the file path (if saved) or the PIL Image.
    """
    W, H = 1050, 450
    PAD = 15  # minimal padding to fill the sticker
    label = Image.new('RGB', (W, H), 'white')
    draw = ImageDraw.Draw(label)

    # --- Left side: QR code (square, fills height) ---
    qr_size = H - 2 * PAD  # 420px
    qr_img = generate_qr_code(barcode_value, size=qr_size)
    qr_x = PAD
    qr_y = PAD
    label.paste(qr_img, (qr_x, qr_y))

    # --- Right side: name + barcode + ID text ---
    right_x = qr_x + qr_size + PAD
    right_w = W - right_x - PAD

    # Barcode ID text font — large and prominent
    font_id = _find_font(MONO_BOLD_FONTS, 44)
    id_bbox = draw.textbbox((0, 0), barcode_value, font=font_id)
    id_text_w = id_bbox[2] - id_bbox[0]
    id_h = id_bbox[3] - id_bbox[1]

    # Device name — dynamically size to fit right-side width, centered
    for size in range(44, 18, -2):
        font_name = _find_font(BOLD_FONTS, size)
        bbox = draw.textbbox((0, 0), device_name, font=font_name)
        if bbox[2] - bbox[0] <= right_w:
            break
    name_text_w = bbox[2] - bbox[0]
    name_h = bbox[3] - bbox[1]

    # Vertical layout within right side
    gap = 8
    total_text_h = name_h + gap + id_h  # name + gap + id text
    barcode_h = H - 2 * PAD - total_text_h - 2 * gap
    if barcode_h < 100:
        barcode_h = 100

    # Vertically center the whole right-side block
    block_h = name_h + gap + barcode_h + gap + id_h
    top_y = (H - block_h) // 2

    name_y = top_y
    barcode_y = name_y + name_h + gap
    id_y = barcode_y + barcode_h + gap

    # Draw device name — centered over barcode area
    name_x = right_x + (right_w - name_text_w) // 2
    name_bearing = draw.textbbox((name_x, name_y), device_name, font=font_name)[0] - name_x
    draw.text((name_x - name_bearing, name_y), device_name, fill='black', font=font_name)

    # Draw Code 128 barcode — full right-side width, crisp
    try:
        barcode_img = generate_barcode_image(barcode_value, width=right_w, height=barcode_h)
        label.paste(barcode_img, (right_x, barcode_y))
    except Exception:
        draw.text((right_x, barcode_y + 20), barcode_value, fill='black', font=font_id)

    # Draw barcode ID text — centered under barcode
    id_x = right_x + (right_w - id_text_w) // 2
    draw.text((id_x, id_y), barcode_value, fill='black', font=font_id)

    if save:
        _ensure_labels_dir()
        path = os.path.join(LABELS_DIR, f'{device_id}.png')
        label.save(path, 'PNG')
        return path
    else:
        return label


def generate_label_sheet(devices, cols=3, rows=6):
    """
    Generate a US Letter page (2550x3300 px at 300 DPI) with a grid of labels.
    Each label is 1050x450 px (3.5x1.5 at 300 DPI).

    Args:
        devices: list of dicts with device_id, barcode_value, name keys
        cols: number of columns (default 3)
        rows: number of rows (default 6)

    Returns: PIL Image of the full sheet
    """
    page_w, page_h = 2550, 3300
    margin = 75

    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin
    cell_w = usable_w // cols
    cell_h = usable_h // rows

    sheet = Image.new('RGB', (page_w, page_h), 'white')

    for i, device in enumerate(devices[:cols * rows]):
        col = i % cols
        row = i // cols

        label_img = generate_label(
            device['device_id'],
            device['barcode_value'],
            device['name'],
            save=False,
        )

        scale = min(cell_w / label_img.width, cell_h / label_img.height)
        scaled_w = int(label_img.width * scale)
        scaled_h = int(label_img.height * scale)
        scaled_img = label_img.resize((scaled_w, scaled_h), Image.LANCZOS)

        cell_x = margin + col * cell_w
        cell_y = margin + row * cell_h
        offset_x = cell_x + (cell_w - scaled_w) // 2
        offset_y = cell_y + (cell_h - scaled_h) // 2

        sheet.paste(scaled_img, (offset_x, offset_y))

    return sheet


def label_exists(device_id):
    """Check if a label PNG already exists for this device."""
    return os.path.isfile(os.path.join(LABELS_DIR, f'{device_id}.png'))


def get_label_path(device_id):
    """Get the file path for a device's label PNG."""
    return os.path.join(LABELS_DIR, f'{device_id}.png')

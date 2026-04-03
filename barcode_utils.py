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
    Create a 1800x1200 pixel device label (4x6 inches at 300 DPI) containing:
    - Left: QR code (500x500, centered vertically)
    - Right: Device name, barcode value, team name, Code 128 barcode
    - 2px gray border

    If save=True, writes PNG to static/labels/{device_id}.png.
    Returns the file path (if saved) or the PIL Image.
    """
    W, H = 1800, 1200
    label = Image.new('RGB', (W, H), 'white')
    draw = ImageDraw.Draw(label)

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

    font_name = _find_font(["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
                             "Helvetica-Bold.ttf", "Helvetica.ttc", "Arial Bold.ttf"], 96)
    font_barcode_id = _find_font(["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf",
                                   "Courier.ttc", "Menlo.ttc", "Courier New Bold.ttf"], 64)
    font_team = _find_font(["DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                             "Helvetica.ttc", "Helvetica-Light.ttf", "Arial.ttf"], 52)

    # --- Left side: QR code ---
    qr_size = 700
    qr_img = generate_qr_code(barcode_value, size=qr_size)
    qr_y = (H - qr_size) // 2
    label.paste(qr_img, (50, qr_y))

    # --- Right side: text and barcode ---
    right_x = 820
    text_w = W - right_x - 50  # available width for text/barcode

    # Device name (bold, up to 2 lines)
    display_name = device_name[:40] if len(device_name) <= 40 else device_name[:37] + '...'
    draw.text((right_x, 60), display_name, fill='black', font=font_name)

    # Barcode value (monospace, bold)
    draw.text((right_x, 190), barcode_value, fill='#222222', font=font_barcode_id)

    # Team name
    draw.text((right_x, 280), 'HP Connectivity Team', fill='#666666', font=font_team)

    # Code 128 barcode image — fill remaining space
    try:
        barcode_img = generate_barcode_image(barcode_value, width=text_w, height=400)
        label.paste(barcode_img, (right_x, 400))
    except Exception:
        draw.text((right_x, 500), barcode_value, fill='black', font=font_barcode_id)

    # 3px gray border around the entire label
    draw.rectangle([0, 0, W - 1, H - 1], outline='#cccccc', width=3)

    if save:
        _ensure_labels_dir()
        path = os.path.join(LABELS_DIR, f'{device_id}.png')
        label.save(path, 'PNG')
        return path
    else:
        return label


def generate_label_sheet(devices, cols=2, rows=4):
    """
    Generate a US Letter page (2550x3300 px at 300 DPI) with a grid of labels.
    Each label is 1800x1200 px (4x6 at 300 DPI), scaled to fit grid cells.

    Args:
        devices: list of dicts with device_id, barcode_value, name keys
        cols: number of columns (default 2)
        rows: number of rows (default 4)

    Returns: PIL Image of the full sheet
    """
    page_w, page_h = 2550, 3300
    margin = 75

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

        # Scale label to fit within cell while maintaining aspect ratio
        scale = min(cell_w / label_img.width, cell_h / label_img.height)
        scaled_w = int(label_img.width * scale)
        scaled_h = int(label_img.height * scale)
        scaled_img = label_img.resize((scaled_w, scaled_h), Image.LANCZOS)

        # Center scaled label within cell
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

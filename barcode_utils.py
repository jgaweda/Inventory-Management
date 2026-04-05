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


def generate_qr_code(data, size=250):
    """
    Generate a QR code image for the given data string.
    Returns a PIL Image at exactly size x size pixels.

    Calculates a box_size that divides evenly into the target size so
    no fractional-pixel interpolation occurs — every QR module maps to
    a whole number of pixels, producing perfectly crisp edges.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    # Calculate modules: matrix size + 2*border
    modules = qr.modules_count + 2 * 2
    # Find largest box_size that divides evenly into target size
    box_size = size // modules
    if box_size < 1:
        box_size = 1
    qr.box_size = box_size
    img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    # The native size is box_size * modules — center-crop or pad to exact target
    native = box_size * modules
    if native == size:
        return img
    elif native < size:
        # Pad with white to center
        result = Image.new('RGB', (size, size), 'white')
        offset = (size - native) // 2
        result.paste(img, (offset, offset))
        return result
    else:
        # Slightly larger — crop from center
        offset = (native - size) // 2
        return img.crop((offset, offset, offset + size, offset + size))


def generate_barcode_image(data, width=350, height=80):
    """
    Generate a Code 128 barcode image (no human-readable text below).
    Returns a PIL Image sized to width x height.

    Renders with minimal quiet zone (2mm), crops to tight bounding box,
    then scales to fill the exact target. This maximizes bar thickness
    and minimizes whitespace. The label layout provides additional quiet
    zone via the gap between the QR code and label edge.
    """
    writer = ImageWriter()
    code = Code128(data, writer=writer)
    buffer = io.BytesIO()
    code.render(writer_options={
        'font_size': 0,
        'text_distance': 0,
        'quiet_zone': 2.0,       # minimal — label edges provide the rest
        'module_width': 0.5,     # render small, then scale up
        'module_height': 30,
        'dpi': 300,
    }).save(buffer, format='PNG')
    buffer.seek(0)

    img = Image.open(buffer).convert('RGB')

    # Crop to tight bounding box around the actual bars, then add back
    # a small quiet zone (10px each side). This ensures bars fill most
    # of the target width rather than having oversized quiet zones.
    gray = img.convert('L')
    bbox = gray.point(lambda x: 0 if x > 200 else 255).getbbox()
    if bbox:
        qz = 10  # minimal quiet zone in pixels
        x0 = max(0, bbox[0] - qz)
        x1 = min(img.width, bbox[2] + qz)
        img = img.crop((x0, bbox[1], x1, bbox[3]))

    # Scale to fill target exactly — NEAREST preserves crisp bar edges
    img = img.resize((width, height), Image.NEAREST)
    return img


def _fit_font(draw, text, font_names, max_width, max_size, min_size=20):
    """Find the largest font size that fits text within max_width.

    Returns (font, display_text, text_width, text_height). If text
    doesn't fit at min_size, it's truncated with '...' to guarantee
    readability. min_size 20px ~ 6pt at 300 DPI, the smallest reliably
    legible on a printed label.
    """
    for size in range(max_size, min_size - 1, -2):
        font = _find_font(font_names, size)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= max_width:
            return font, text, tw, th
    # At min_size — truncate with ellipsis if needed
    font = _find_font(font_names, min_size)
    display = text
    while len(display) > 4:
        bbox = draw.textbbox((0, 0), display, font=font)
        if bbox[2] - bbox[0] <= max_width:
            break
        display = display[:-2] + '\u2026'
    bbox = draw.textbbox((0, 0), display, font=font)
    return font, display, bbox[2] - bbox[0], bbox[3] - bbox[1]


def generate_label(device_id, barcode_value, device_name, save=True):
    """
    Create a 1050x450 pixel device label (3.5x1.5 inches at 300 DPI, landscape).

    Layout (minimal whitespace, three horizontal bands):
      TOP    — device name (full label width, centered)
      MIDDLE — QR code (left) + Code 128 barcode (right), same height
      BOTTOM — barcode ID (full label width, centered)

    Text bands span the full label width so no horizontal space is wasted.
    QR and barcode share the middle band at equal height. Whitespace is
    minimized everywhere.

    If save=True, writes PNG to static/labels/{device_id}.png.
    Returns the file path (if saved) or the PIL Image.
    """
    W, H = 1050, 450
    EDGE = 6          # minimal edge margin
    TEXT_PAD_Y = 5    # vertical padding inside text bands
    QR_GAP = 8        # gap between QR and barcode

    label = Image.new('RGB', (W, H), 'white')
    draw = ImageDraw.Draw(label)

    # --- Measure text bands (full label width) ---
    usable_w = W - 2 * EDGE
    font_name, display_name, name_tw, name_th = _fit_font(
        draw, device_name, BOLD_FONTS, usable_w - 20, 36, min_size=20)
    font_id, display_id, id_tw, id_th = _fit_font(
        draw, barcode_value, MONO_BOLD_FONTS, usable_w - 20, 42, min_size=24)

    top_band_h = name_th + 2 * TEXT_PAD_Y
    bot_band_h = id_th + 2 * TEXT_PAD_Y
    mid_h = H - 2 * EDGE - top_band_h - bot_band_h

    # --- TOP band: device name (full width, centered) ---
    top_y = EDGE
    name_x = EDGE + (usable_w - name_tw) // 2
    name_y = top_y + TEXT_PAD_Y
    bearing = draw.textbbox((name_x, name_y), display_name, font=font_name)[0] - name_x
    draw.text((name_x - bearing, name_y), display_name, fill='black', font=font_name)

    # --- MIDDLE band: QR (left) + barcode (right), same height ---
    mid_y = top_y + top_band_h
    qr_size = mid_h  # QR matches barcode height
    qr_img = generate_qr_code(barcode_value, size=qr_size)
    label.paste(qr_img, (EDGE, mid_y))

    bc_x = EDGE + qr_size + QR_GAP
    bc_w = W - bc_x - EDGE
    try:
        barcode_img = generate_barcode_image(barcode_value, width=bc_w, height=mid_h)
        label.paste(barcode_img, (bc_x, mid_y))
    except Exception:
        font_fb = _find_font(MONO_BOLD_FONTS, 36)
        draw.text((bc_x + 20, mid_y + 20), barcode_value, fill='black', font=font_fb)

    # --- BOTTOM band: barcode ID (full width, centered) ---
    bot_y = mid_y + mid_h
    id_x = EDGE + (usable_w - id_tw) // 2
    id_y = bot_y + TEXT_PAD_Y
    draw.text((id_x, id_y), display_id, fill='black', font=font_id)

    # --- Hairline border for cut/peel alignment ---
    draw.rectangle([0, 0, W - 1, H - 1], outline='#cccccc', width=1)

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

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
from runtime_dirs import BUNDLE_DIR, DATA_DIR

# Directory where label PNGs are saved (writable, outside bundled static)
LABELS_DIR = os.path.join(DATA_DIR, 'static', 'labels')

# Bundled fonts directory — ships with the app so labels render consistently
# on all platforms regardless of system-installed fonts
BUNDLED_FONTS_DIR = os.path.join(BUNDLE_DIR, 'fonts')


def _ensure_labels_dir():
    """Create the labels directory if it doesn't exist."""
    os.makedirs(LABELS_DIR, exist_ok=True)


_font_path_cache = {}  # font name list key -> resolved path


def _load_default_font(size):
    """Pillow 10.1+ supports ImageFont.load_default(size=...), but earlier
    versions (including the Pillow 9.x pinned for Python 3.8 / Windows 7)
    raise TypeError on the size kwarg. Fall back gracefully in that case."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _find_font(names, size):
    """Try multiple font paths and return the first that works (path cached).
    Checks bundled fonts/ directory first, then system font locations."""
    cache_key = tuple(names)
    if cache_key in _font_path_cache:
        path = _font_path_cache[cache_key]
        if path is None:
            return _load_default_font(size)
        return ImageFont.truetype(path, size)

    for name in names:
        for path in [
            # Bundled fonts (ships with the app, guaranteed present)
            os.path.join(BUNDLED_FONTS_DIR, name),
            # Linux
            f"/usr/share/fonts/truetype/dejavu/{name}",
            f"/usr/share/fonts/truetype/liberation/{name}",
            # macOS
            f"/System/Library/Fonts/{name}",
            f"/Library/Fonts/{name}",
            f"/System/Library/Fonts/Supplemental/{name}",
            # Windows
            os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', name),
        ]:
            try:
                font = ImageFont.truetype(path, size)
                _font_path_cache[cache_key] = path
                return font
            except (OSError, IOError):
                continue
    # Last resort: try by name only (Pillow searches system paths)
    for name in names:
        try:
            font = ImageFont.truetype(name, size)
            _font_path_cache[cache_key] = name
            return font
        except (OSError, IOError):
            continue
    _font_path_cache[cache_key] = None
    return _load_default_font(size)


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
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=1,
        border=0,
    )
    qr.add_data(data)
    qr.make(fit=True)
    # Calculate modules: matrix size (no internal border — label provides quiet zone)
    modules = qr.modules_count
    # Find largest box_size that divides evenly into target size
    box_size = size // modules
    if box_size < 1:
        box_size = 1
    qr.box_size = box_size
    img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    # The native size is box_size * modules — center-crop or pad to exact target
    native = box_size * modules
    if native == size:
        result = img
    elif native < size:
        # Pad with white to center
        result = Image.new('RGB', (size, size), 'white')
        pad = (size - native) // 2
        result.paste(img, (pad, pad))
    else:
        # Slightly larger — crop from center
        off = (native - size) // 2
        result = img.crop((off, off, off + size, off + size))

    # Embed "JG" initials as QR modules in the center — the letters are
    # formed by the grid squares themselves so they blend into the code.
    # H-level error correction (30%) recovers the altered modules.
    #
    # 7x5 pixel-art pattern (1=black module, 0=white module):
    #   . J . G G G .
    #   . J . G . . .
    #   . J . G . G G
    #   J J . G . . G
    #   J J . G G G .
    _jg_pattern = [
        [0, 1, 0, 1, 1, 1, 0],
        [0, 1, 0, 1, 0, 0, 0],
        [0, 1, 0, 1, 0, 1, 1],
        [1, 1, 0, 1, 0, 0, 1],
        [1, 1, 0, 1, 1, 1, 0],
    ]
    pw, ph = len(_jg_pattern[0]), len(_jg_pattern)
    # Centre the pattern on the QR module grid
    grid_cx = modules // 2
    grid_cy = modules // 2
    start_col = grid_cx - pw // 2
    start_row = grid_cy - ph // 2
    # Compute pixel offset (accounts for padding when native < size)
    if native < size:
        img_offset = (size - native) // 2
    else:
        img_offset = 0
    draw = ImageDraw.Draw(result)
    for r, row in enumerate(_jg_pattern):
        for c, val in enumerate(row):
            mx = start_col + c
            my = start_row + r
            px = img_offset + mx * box_size
            py = img_offset + my * box_size
            color = 'black' if val else 'white'
            draw.rectangle([px, py, px + box_size - 1, py + box_size - 1],
                           fill=color)

    return result


def generate_barcode_image(data, width=350, height=80, tight_crop=False):
    """
    Generate a Code 128 barcode image (no human-readable text below).
    Returns a PIL Image sized to width x height.

    Renders at 2× target resolution and downscales with NEAREST to ensure
    every bar is an integer number of pixels wide — critical for scanner
    readability. module_width is calculated from the target width so bars
    fill the available space without any lossy rescaling.

    If tight_crop=True, the returned image is cropped to just the bars
    with no quiet-zone padding — the caller is responsible for providing
    the required quiet zones via surrounding whitespace.
    """
    writer = ImageWriter()
    code = Code128(data, writer=writer)

    # Code 128: start(11) + data(11 each) + checksum(11) + stop(13) = modules
    # Quiet zone 10 modules each side (GS1 spec).
    # Estimate total modules to compute ideal module_width.
    n_chars = len(data)
    total_modules = 11 + n_chars * 11 + 11 + 13 + 20  # 20 = quiet zones
    # Render at 2× and scale down — ensures bars are whole-pixel aligned
    render_scale = 2
    render_w = width * render_scale
    # module_width in mm at render DPI
    render_dpi = 300
    module_px = render_w / total_modules
    module_mm = module_px / render_dpi * 25.4
    # Clamp: minimum 0.3mm for scanner readability
    module_mm = max(0.3, module_mm)

    buffer = io.BytesIO()
    code.render(writer_options={
        'font_size': 0,
        'text_distance': 0,
        'quiet_zone': module_mm * 10,   # 10× module width per GS1 spec
        'module_width': module_mm,
        'module_height': 50,            # tall bars — easier for hand-held scanners
        'dpi': render_dpi,
    }).save(buffer, format='PNG')
    buffer.seek(0)

    img = Image.open(buffer).convert('RGB')

    # Crop to bounding box; optionally preserve quiet zones
    gray = img.convert('L')
    bbox = gray.point(lambda x: 0 if x > 200 else 255).getbbox()
    if bbox:
        if tight_crop:
            # Crop to just the bars — caller provides quiet zones
            img = img.crop((bbox[0], bbox[1], bbox[2], bbox[3]))
        else:
            bar_width = bbox[2] - bbox[0]
            qz = max(15, int(bar_width * 0.10))
            x0 = max(0, bbox[0] - qz)
            x1 = min(img.width, bbox[2] + qz)
            img = img.crop((x0, bbox[1], x1, bbox[3]))

    # Scale to exact target — NEAREST preserves crisp bar edges
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

    Scanner-optimized layout — Code 128 barcode spans FULL label width:
      TOP ROW  — QR code (left) | device name + barcode ID (right, stacked)
      BOTTOM   — full-width Code 128 barcode (maximum bar thickness)

    Full-width barcode produces ~48% thicker bars vs the old side-by-side
    layout, significantly improving laser scanner readability.

    If save=True, writes PNG to static/labels/{device_id}.png.
    Returns the file path (if saved) or the PIL Image.
    """
    W, H = 1050, 450
    MARGIN = 40       # safe zone for DYMO printer (~0.13" non-printable edges)
    GAP = 14          # gap between QR and text

    label = Image.new('RGB', (W, H), 'white')
    draw = ImageDraw.Draw(label)

    # --- Layout: QR (left) + text (right) top zone, full-width Code 128 bottom ---
    # QR has border=0 — the label surface provides top/left quiet zone.
    # QR left edge aligns with barcode left edge (both at x=MARGIN).
    # DYMO printers clip aggressively at the top edge — use generous top margin.
    QR_TOP = 90                            # top clearance for DYMO clipping
    BC_BOTTOM = 60                         # bottom margin for DYMO clipping
    qr_size = 117                          # ~0.39"
    qr_gap = 24                            # 4 modules × ~5px — spec-compliant
    bc_y = QR_TOP + qr_size + qr_gap      # 231
    bc_h = H - bc_y - BC_BOTTOM           # 159 — shorter barcode, still scannable
    top_zone_h = qr_size                   # text constrained to QR height
    text_area_w = W - MARGIN - qr_size - GAP - MARGIN  # right of QR

    # Text sized to fit within QR code height boundaries
    font_name, display_name, name_tw, name_th = _fit_font(
        draw, device_name, BOLD_FONTS, text_area_w, 45, min_size=14)
    font_id, display_id, id_tw, id_th = _fit_font(
        draw, barcode_value, MONO_BOLD_FONTS, text_area_w, 56, min_size=20)

    # --- TOP ROW: QR code (left) + text info (right) ---
    qr_img = generate_qr_code(barcode_value, size=qr_size)
    label.paste(qr_img, (MARGIN, QR_TOP))

    text_x = MARGIN + qr_size + GAP
    text_cx = text_x + text_area_w // 2
    total_text_h = name_th + 12 + id_th
    text_top = QR_TOP + (top_zone_h - total_text_h) // 2
    draw.text((text_cx, text_top + name_th // 2), display_name,
              fill='black', font=font_name, anchor='mm')
    draw.text((text_cx, text_top + name_th + 12 + id_th // 2), display_id,
              fill='black', font=font_id, anchor='mm')

    # --- BOTTOM: Code 128 barcode, full width ---
    bc_w = W - 2 * MARGIN
    try:
        barcode_img = generate_barcode_image(barcode_value, width=bc_w, height=bc_h,
                                             tight_crop=True)
        label.paste(barcode_img, (MARGIN, bc_y))
    except Exception:
        font_fb = _find_font(MONO_BOLD_FONTS, 36)
        draw.text((20, bc_y + 20), barcode_value, fill='black', font=font_fb)

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
        scaled_img = label_img.resize((scaled_w, scaled_h), Image.NEAREST)

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

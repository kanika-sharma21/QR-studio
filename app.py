from flask import Flask, render_template, request, send_file, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename
from PIL import Image
import qrcode
from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H
from io import BytesIO
from pathlib import Path
from datetime import datetime
import json, csv, zipfile, uuid, re

app = Flask(__name__)
app.secret_key = "change-this-secret-key-before-deployment"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "generated"
UPLOAD_DIR = BASE_DIR / "uploads"
HISTORY_FILE = BASE_DIR / "history.json"
OUTPUT_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_LOGOS = {".png", ".jpg", ".jpeg", ".webp"}
ERROR_LEVELS = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}
MAX_BATCH_ITEMS = 100

def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    except (json.JSONDecodeError, OSError):
        return []

def save_history(items):
    HISTORY_FILE.write_text(json.dumps(items[:200], indent=2, ensure_ascii=False), encoding="utf-8")

def safe_color(value, fallback):
    value = (value or "").strip()
    return value if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else fallback

def clean_filename(value):
    value = secure_filename(value or "") or "qr-code"
    return value[:70]

def make_qr(data, fg="#111827", bg="#FFFFFF", error="H", box_size=10, border=4, logo_path=None):
    if not data or not data.strip():
        raise ValueError("Please enter text or a URL first.")
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_LEVELS.get(error, ERROR_CORRECT_H),
        box_size=max(4, min(int(box_size), 20)),
        border=max(2, min(int(border), 10)),
    )
    qr.add_data(data.strip())
    qr.make(fit=True)
    image = qr.make_image(fill_color=fg, back_color=bg).convert("RGB")

    if logo_path:
        try:
            with Image.open(logo_path) as logo:
                logo = logo.convert("RGBA")
                max_logo = max(24, image.width // 5)
                logo.thumbnail((max_logo, max_logo), Image.Resampling.LANCZOS)
                # Put the logo on an opaque white tile to preserve contrast.
                tile_pad = max(6, image.width // 80)
                tile = Image.new("RGBA", (logo.width + tile_pad*2, logo.height + tile_pad*2), "white")
                tile.alpha_composite(logo, (tile_pad, tile_pad))
                x = (image.width - tile.width) // 2
                y = (image.height - tile.height) // 2
                image = image.convert("RGBA")
                image.alpha_composite(tile, (x, y))
                image = image.convert("RGB")
        except Exception as exc:
            raise ValueError(f"Could not use that logo image: {exc}")
    return image

def save_image(image, filename, fmt="PNG"):
    fmt = fmt.upper()
    if fmt not in {"PNG", "JPEG"}:
        fmt = "PNG"
    path = OUTPUT_DIR / f"{filename}.{fmt.lower()}"
    if fmt == "JPEG":
        image.convert("RGB").save(path, format="JPEG", quality=95)
    else:
        image.save(path, format="PNG", optimize=True)
    return path

def add_history(data, filename, fg, bg, error, fmt):
    items = load_history()
    items.insert(0, {
        "id": str(uuid.uuid4()),
        "data": data[:500],
        "filename": filename,
        "foreground": fg,
        "background": bg,
        "error_correction": error,
        "format": fmt,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save_history(items)

@app.route("/")
def index():
    return render_template("index.html", history=load_history()[:30])

@app.post("/generate")
def generate():
    data = request.form.get("data", "").strip()
    fg = safe_color(request.form.get("foreground"), "#111827")
    bg = safe_color(request.form.get("background"), "#FFFFFF")
    error = request.form.get("error_correction", "H")
    fmt = request.form.get("format", "PNG").upper()
    filename = clean_filename(request.form.get("filename", "qr-studio-code"))
    logo = request.files.get("logo")
    logo_path = None

    try:
        if logo and logo.filename:
            ext = Path(secure_filename(logo.filename)).suffix.lower()
            if ext not in ALLOWED_LOGOS:
                raise ValueError("Logo must be PNG, JPG, JPEG, or WEBP.")
            logo_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
            logo.save(logo_path)
        image = make_qr(data, fg, bg, error, request.form.get("box_size", 10), request.form.get("border", 4), logo_path)
        path = save_image(image, filename, fmt)
        add_history(data, filename, fg, bg, error, fmt)
        flash(f"QR code generated: {path.name}", "success")
        return send_file(path, as_attachment=True, download_name=path.name)
    except (ValueError, OSError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))
    finally:
        if logo_path and logo_path.exists():
            logo_path.unlink(missing_ok=True)

@app.post("/batch")
def batch():
    source = request.files.get("batch_file")
    if not source or not source.filename:
        flash("Choose a .txt or .csv file for batch generation.", "error")
        return redirect(url_for("index"))
    ext = Path(secure_filename(source.filename)).suffix.lower()
    if ext not in {".txt", ".csv"}:
        flash("Batch input must be a .txt or .csv file.", "error")
        return redirect(url_for("index"))

    fg = safe_color(request.form.get("batch_foreground"), "#111827")
    bg = safe_color(request.form.get("batch_background"), "#FFFFFF")
    error = request.form.get("batch_error", "H")
    zip_buffer = BytesIO()
    created = 0
    try:
        raw = source.read().decode("utf-8-sig")
        if ext == ".csv":
            rows = list(csv.reader(raw.splitlines()))
            values = [row[0].strip() for row in rows if row and row[0].strip()]
            # Treat a first-column header named data/text/url as a header.
            if values and values[0].lower() in {"data", "text", "url", "content"}:
                values = values[1:]
        else:
            values = [line.strip() for line in raw.splitlines() if line.strip()]
        values = values[:MAX_BATCH_ITEMS]
        if not values:
            raise ValueError("No usable entries were found in that file.")
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, value in enumerate(values, start=1):
                image = make_qr(value, fg, bg, error)
                name = f"qr-{i:03d}.png"
                buffer = BytesIO()
                image.save(buffer, format="PNG")
                zf.writestr(name, buffer.getvalue())
                add_history(value, f"qr-{i:03d}", fg, bg, error, "PNG")
                created += 1
        zip_buffer.seek(0)
        return send_file(zip_buffer, mimetype="application/zip", as_attachment=True, download_name="qr-studio-batch.zip")
    except (ValueError, UnicodeDecodeError, OSError, csv.Error) as exc:
        flash(f"Batch generation failed: {exc}", "error")
        return redirect(url_for("index"))

@app.get("/history")
def history():
    return jsonify(load_history()[:200])

@app.post("/history/clear")
def clear_history():
    save_history([])
    flash("Local history cleared.", "success")
    return redirect(url_for("index"))

@app.get("/download/<path:filename>")
def download(filename):
    # Only allow files directly inside generated/ and with a basename.
    safe = Path(filename).name
    path = OUTPUT_DIR / safe
    if not path.exists() or path.parent != OUTPUT_DIR:
        flash("That file could not be found.", "error")
        return redirect(url_for("index"))
    return send_file(path, as_attachment=True, download_name=safe)

if __name__ == "__main__":
    app.run(debug=True)

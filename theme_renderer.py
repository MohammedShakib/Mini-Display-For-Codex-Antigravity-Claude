import math
import time
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BASE_DIR = Path(__file__).parent.resolve()
ASSETS_DIR = BASE_DIR / "assets"

def font(size, bold=False):
    names = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()

def mono(size, bold=False):
    names = [
        "C:/Windows/Fonts/consolab.ttf" if bold else "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/courbd.ttf" if bold else "C:/Windows/Fonts/cour.ttf",
    ]
    for n in names:
        if Path(n).exists():
            return ImageFont.truetype(n, size)
    return font(size, bold)

def load_logo(path, size):
    if not Path(path).exists():
        return None
    img = Image.open(path).convert("RGBA")
    
    if "antigravity" in Path(path).name.lower():
        cleaned = []
        for r, g, b, a in img.getdata():
            cleaned.append((r, g, b, 0 if r < 8 and g < 8 and b < 8 else a))
        img.putdata(cleaned)
        
    alpha_box = img.getchannel("A").getbbox()
    if alpha_box:
        img = img.crop(alpha_box)
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    return img

def paste_logo(canvas, logo, cx, cy):
    if logo is None:
        return
    x = cx - logo.width // 2
    y = cy - logo.height // 2
    canvas.alpha_composite(logo, (x, y))

def draw_arc(draw, cx, cy, r, start, end, color, width=10):
    x0, y0 = cx - r, cy - r
    x1, y1 = cx + r, cy + r
    draw.arc([x0, y0, x1, y1], start=start, end=end, fill=color, width=width)

def draw_circle_arc(draw, cx, cy, r, pct, color, bg_color=(30,40,55), width=12):
    draw_arc(draw, cx, cy, r, -90, 270, bg_color, width)
    if pct is not None and pct > 0:
        end = -90 + int(360 * pct)
        draw_arc(draw, cx, cy, r, -90, end, color, width)

def format_age(timestamp):
    if not timestamp:
        return ""
    diff = int(time.time() - timestamp)
    if diff < 60:
        return "now"
    mins = diff // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    return f"{hours}h ago"

def clamp_percent(p):
    if p is None:
        return None
    return max(0.0, min(1.0, float(p) / 100.0))

# =========================================================================
# THEME RENDERERS
# =========================================================================

def render_orbital(pct5h, pctW, model_label, age_str, status_color, is_warning, is_stale, is_offline, reset_str):
    w, h = 480, 480
    scale = 2.0
    is_ag = "ANTIGRAVITY" in str(model_label).upper() or "GEMINI" in str(model_label).upper() or "CLAUDE" in str(model_label).upper()

    # 1. Base image: deep midnight blue
    bg_color = (4, 8, 18, 255)
    img = Image.new("RGBA", (w, h), bg_color)
    d = ImageDraw.Draw(img)

    # 2. Outer border with neon blue glow (multi-layer for premium feel)
    border_rad = int(28 * scale)
    margin = int(5 * scale)
    # Outer glow layer (wider, softer)
    glow_box = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gbd = ImageDraw.Draw(glow_box)
    gbd.rounded_rectangle((margin, margin, w - margin, h - margin), radius=border_rad, outline=(0, 100, 255, 60), width=int(5 * scale))
    glow_box = glow_box.filter(ImageFilter.GaussianBlur(int(8 * scale)))
    img.alpha_composite(glow_box)
    # Inner glow layer (tighter, brighter)
    glow_box2 = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gbd2 = ImageDraw.Draw(glow_box2)
    gbd2.rounded_rectangle((margin, margin, w - margin, h - margin), radius=border_rad, outline=(0, 130, 255, 100), width=int(3 * scale))
    glow_box2 = glow_box2.filter(ImageFilter.GaussianBlur(int(4 * scale)))
    img.alpha_composite(glow_box2)

    # Sharp inner border
    d.rounded_rectangle((margin, margin, w - margin, h - margin), radius=border_rad, outline=(20, 60, 130, 200), width=int(1.5 * scale))

    # 3. Status LED top right
    led_x = int(218 * scale)
    led_y = int(22 * scale)
    led_r = int(5 * scale)
    led_color = (0, 235, 120) if not is_offline and not is_stale else ((255, 180, 0) if is_stale else (90, 100, 110))
    if is_warning and not is_offline:
        led_color = (255, 60, 80)

    led_glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    lgd = ImageDraw.Draw(led_glow)
    lgd.ellipse((led_x - led_r*3, led_y - led_r*3, led_x + led_r*3, led_y + led_r*3), fill=(*led_color, 120))
    led_glow = led_glow.filter(ImageFilter.GaussianBlur(int(5 * scale)))
    img.alpha_composite(led_glow)
    d.ellipse((led_x - led_r, led_y - led_r, led_x + led_r, led_y + led_r), fill=(*led_color, 255))
    d.ellipse((led_x - int(led_r*0.4), led_y - int(led_r*0.4), led_x + int(led_r*0.4), led_y + int(led_r*0.4)), fill=(255, 255, 255, 220))

    # 4. Header (Centered at top) - use original logo assets
    f_header = font(int(13 * scale), True)
    if not is_ag:
        hp = ASSETS_DIR / "codex_orbital_header.png"
        if hp.exists():
            h_img = Image.open(hp).convert("RGBA")
            target_h = int(20 * scale)
            target_w = int(h_img.width * target_h / h_img.height)
            h_img = h_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            hx = int((w - target_w) / 2)
            hy = int(12 * scale)
            img.alpha_composite(h_img, (hx, hy))
        else:
            d.text((w // 2, int(22 * scale)), "C O D E X", fill=(255, 255, 255), font=f_header, anchor="mm")
    else:
        hp = ASSETS_DIR / "ag_white_logo.png"
        target_h = int(16 * scale)
        if hp.exists():
            ag_icon = Image.open(hp).convert("RGBA")
            ag_w = int(ag_icon.width * target_h / ag_icon.height)
            ag_icon = ag_icon.resize((ag_w, target_h), Image.Resampling.LANCZOS)
            label_text = "G E M I N I" if "GEMINI" in model_label.upper() else "C L A U D E"
            bbox = d.textbbox((0, 0), label_text, font=f_header)
            total_w = ag_w + int(8 * scale) + (bbox[2] - bbox[0])
            start_x = int((w - total_w) / 2)
            img.alpha_composite(ag_icon, (start_x, int(14 * scale)))
            d.text((start_x + ag_w + int(8 * scale), int(22 * scale)), label_text, fill=(255, 255, 255), font=f_header, anchor="lm")
        else:
            d.text((w // 2, int(22 * scale)), model_label, fill=(255, 255, 255), font=f_header, anchor="mm")

    # 5. Rings Center & Radii (larger, more prominent)
    CX, CY = int(120 * scale), int(118 * scale)
    R_OUT = int(80 * scale)
    R_IN  = int(56 * scale)
    W_OUT = int(8 * scale)
    W_IN  = int(7 * scale)

    # Subtle dark disc behind rings with soft edge
    dial_r = int(92 * scale)
    d.ellipse([CX - dial_r, CY - dial_r, CX + dial_r, CY + dial_r], fill=(2, 6, 18, 200))
    d.ellipse([CX - dial_r, CY - dial_r, CX + dial_r, CY + dial_r], outline=(12, 30, 65, 80), width=int(1.5 * scale))

    # Outer ring background track (full 360)
    d.arc([CX - R_OUT, CY - R_OUT, CX + R_OUT, CY + R_OUT], start=0, end=360, fill=(18, 36, 68, 200), width=W_OUT)
    # Inner ring background track (full 360)
    d.arc([CX - R_IN, CY - R_IN, CX + R_IN, CY + R_IN], start=0, end=360, fill=(14, 40, 34, 200), width=W_IN)

    # Glow layers for active arcs
    rglow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    rgd = ImageDraw.Draw(rglow)

    # Start from top (-90 degrees) for a cleaner look
    ANG_START = -90

    # Outer Cyan Arc (5H usage)
    if pct5h is not None and pct5h > 0:
        sweep_p = max(4, int(360 * min(1.0, pct5h)))
        end_p = ANG_START + sweep_p

        # Wide soft glow
        rgd.arc([CX - R_OUT, CY - R_OUT, CX + R_OUT, CY + R_OUT], start=ANG_START, end=end_p, fill=(0, 180, 255, 100), width=int(W_OUT + 10 * scale))
        # Medium glow
        rgd.arc([CX - R_OUT, CY - R_OUT, CX + R_OUT, CY + R_OUT], start=ANG_START, end=end_p, fill=(0, 200, 255, 160), width=int(W_OUT + 5 * scale))
        # Core bright arc
        d.arc([CX - R_OUT, CY - R_OUT, CX + R_OUT, CY + R_OUT], start=ANG_START, end=end_p, fill=(0, 215, 255, 255), width=W_OUT)

        # Rounded cap at start
        ang0_rad = math.radians(ANG_START)
        cap_r = W_OUT / 2.0
        sx0 = CX + int(R_OUT * math.cos(ang0_rad))
        sy0 = CY + int(R_OUT * math.sin(ang0_rad))
        d.ellipse([sx0 - cap_r, sy0 - cap_r, sx0 + cap_r, sy0 + cap_r], fill=(0, 215, 255, 255))

        # Glowing bead at leading tip
        ang_rad = math.radians(end_p)
        bx = CX + int(R_OUT * math.cos(ang_rad))
        by = CY + int(R_OUT * math.sin(ang_rad))
        bead_r = int(5 * scale)
        # Outer glow for bead
        rgd.ellipse([bx - int(12 * scale), by - int(12 * scale), bx + int(12 * scale), by + int(12 * scale)], fill=(0, 200, 255, 180))
        # Core bead
        d.ellipse([bx - bead_r, by - bead_r, bx + bead_r, by + bead_r], fill=(200, 240, 255, 255))
        d.ellipse([bx - int(bead_r*0.5), by - int(bead_r*0.5), bx + int(bead_r*0.5), by + int(bead_r*0.5)], fill=(255, 255, 255, 255))

    # Inner Green Arc (Weekly usage)
    if pctW is not None and pctW > 0:
        sweep_w = max(4, int(360 * min(1.0, pctW)))
        end_w = ANG_START + sweep_w

        # Wide soft glow
        rgd.arc([CX - R_IN, CY - R_IN, CX + R_IN, CY + R_IN], start=ANG_START, end=end_w, fill=(0, 220, 100, 100), width=int(W_IN + 9 * scale))
        # Medium glow
        rgd.arc([CX - R_IN, CY - R_IN, CX + R_IN, CY + R_IN], start=ANG_START, end=end_w, fill=(0, 240, 120, 160), width=int(W_IN + 4 * scale))
        # Core bright arc
        d.arc([CX - R_IN, CY - R_IN, CX + R_IN, CY + R_IN], start=ANG_START, end=end_w, fill=(0, 245, 130, 255), width=W_IN)

        # Rounded cap at start
        ang0_in = math.radians(ANG_START)
        cap_r2 = W_IN / 2.0
        sx1 = CX + int(R_IN * math.cos(ang0_in))
        sy1 = CY + int(R_IN * math.sin(ang0_in))
        d.ellipse([sx1 - cap_r2, sy1 - cap_r2, sx1 + cap_r2, sy1 + cap_r2], fill=(0, 245, 130, 255))

        # Glowing bead at leading tip
        ang_rad2 = math.radians(end_w)
        bx2 = CX + int(R_IN * math.cos(ang_rad2))
        by2 = CY + int(R_IN * math.sin(ang_rad2))
        bead_r2 = int(4 * scale)
        rgd.ellipse([bx2 - int(10 * scale), by2 - int(10 * scale), bx2 + int(10 * scale), by2 + int(10 * scale)], fill=(0, 240, 120, 180))
        d.ellipse([bx2 - bead_r2, by2 - bead_r2, bx2 + bead_r2, by2 + bead_r2], fill=(200, 255, 220, 255))
        d.ellipse([bx2 - int(bead_r2*0.5), by2 - int(bead_r2*0.5), bx2 + int(bead_r2*0.5), by2 + int(bead_r2*0.5)], fill=(255, 255, 255, 255))

    rglow = rglow.filter(ImageFilter.GaussianBlur(int(5 * scale)))
    img.alpha_composite(rglow)

    # 6. Text labels inside rings (percentages only, no redundant branding)
    f_lbl = font(int(11 * scale), True)
    f_val = font(int(16 * scale), True)

    # 5H label and value
    d.text((CX, CY - int(30 * scale)), "5H", fill=(0, 215, 255), font=f_lbl, anchor="mm")
    val5_str = f"{int(pct5h*100)}%" if pct5h is not None else "--%"
    d.text((CX, CY - int(15 * scale)), val5_str, fill=(255, 255, 255), font=f_val, anchor="mm")

    # W label and value
    d.text((CX, CY + int(3 * scale)), "W", fill=(0, 245, 130), font=f_lbl, anchor="mm")
    valW_str = f"{int(pctW*100)}%" if pctW is not None else "--%"
    d.text((CX, CY + int(18 * scale)), valW_str, fill=(255, 255, 255), font=f_val, anchor="mm")

    # 7. Footer (no logo inside rings - header already has branding)
    f_footer = font(int(9 * scale))
    footer_y = int(216 * scale)
    color_foot = (130, 155, 190)

    # Clock icon
    r_ico = int(5 * scale)
    cx_ico = int(36 * scale)
    d.ellipse((cx_ico - r_ico, footer_y - r_ico, cx_ico + r_ico, footer_y + r_ico), outline=color_foot, width=int(1.2 * scale))
    d.line([(cx_ico, footer_y), (cx_ico, footer_y - int(3 * scale))], fill=color_foot, width=int(1.2 * scale))
    d.line([(cx_ico, footer_y), (cx_ico + int(2.5 * scale), footer_y)], fill=color_foot, width=int(1.2 * scale))
    d.text((cx_ico + int(8 * scale), footer_y), reset_str, fill=color_foot, font=f_footer, anchor="lm")

    # Divider |
    d.text((int(132 * scale), footer_y), "|", fill=(35, 55, 85), font=f_footer, anchor="mm")

    # Refresh icon
    rx_ico = int(152 * scale)
    d.arc((rx_ico - r_ico, footer_y - r_ico, rx_ico + r_ico, footer_y + r_ico), start=-45, end=225, fill=color_foot, width=int(1.2 * scale))
    d.polygon([(rx_ico + int(3 * scale), footer_y - int(4.5 * scale)), (rx_ico + int(6.5 * scale), footer_y - int(3 * scale)), (rx_ico + int(3 * scale), footer_y - int(1.5 * scale))], fill=color_foot)
    d.text((rx_ico + int(8 * scale), footer_y), age_str, fill=color_foot, font=f_footer, anchor="lm")

    final_img = img.resize((240, 240), Image.Resampling.LANCZOS)
    return final_img


def render_custom_theme(theme_id, logo_path, model_label, used_p, used_w, is_offline, is_stale, last_success_ts, output_path, alert_threshold=80.0, reset_str="Reset 10:25 PM"):
    pct_p = clamp_percent(used_p)
    pct_w = clamp_percent(used_w)

    is_warning = (pct_p is not None and pct_p >= float(alert_threshold) / 100.0) or (pct_w is not None and pct_w >= float(alert_threshold) / 100.0)

    if is_offline:
        status_color = (100, 100, 100)
    elif is_warning:
        status_color = (255, 65, 85)
    elif is_stale:
        status_color = (255, 193, 7)
    else:
        status_color = (0, 220, 80)

    age_str = format_age(last_success_ts)
    if is_stale:
        age_str = f"⚠ {age_str}"

    out_img = render_orbital(pct_p, pct_w, model_label, age_str, status_color, is_warning, is_stale, is_offline, reset_str)
    out_img.convert("RGB").save(output_path, "JPEG", quality=95)
    return True



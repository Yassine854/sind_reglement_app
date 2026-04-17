from fastapi import FastAPI, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import re, json
from pathlib import Path

app = FastAPI(title="Analyse Règlements CAM")

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

TARGET_CAMS = [
    "cam01","cam02","cam03","cam04","cam05","cam06",
    "cam36","cam37","cam38","cam48","cam49"
]
TYPE_MAP = {"CESP": "Espèces", "CTRT": "Traite", "CCHQR": "Chèque"}
TYPE_KEYS = ["Espèces", "Traite", "Chèque"]


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8", errors="replace")
    lines = [l.strip() for l in content.splitlines() if l.strip()]

    data = {cam: {t: {"sum": 0.0, "count": 0} for t in TYPE_KEYS} for cam in TARGET_CAMS}
    skipped = 0

    for line in lines:
        parts = line.split(";")
        if len(parts) < 11:
            skipped += 1
            continue

        ref0 = parts[0].strip()
        ref1 = parts[1].strip()
        amt_str = parts[-1].strip()

        pay_type = None
        for prefix, label in TYPE_MAP.items():
            if ref0.upper().startswith(prefix):
                pay_type = label
                break
        if not pay_type:
            skipped += 1
            continue

        cam_match = re.search(r"CAM(\d+)", ref1, re.IGNORECASE)
        if not cam_match:
            skipped += 1
            continue

        num = cam_match.group(1).zfill(2)
        cam_key = f"cam{num}"
        if cam_key not in TARGET_CAMS:
            skipped += 1
            continue

        try:
            amt = float(amt_str.replace(",", "."))
        except ValueError:
            skipped += 1
            continue

        data[cam_key][pay_type]["sum"] += amt
        data[cam_key][pay_type]["count"] += 1

    rows = []
    for cam in TARGET_CAMS:
        total = sum(data[cam][t]["sum"] for t in TYPE_KEYS)
        total_count = sum(data[cam][t]["count"] for t in TYPE_KEYS)
        if total > 0:
            rows.append({
                "cam": cam.upper(),
                "esp": round(data[cam]["Espèces"]["sum"], 3),
                "trt": round(data[cam]["Traite"]["sum"], 3),
                "chq": round(data[cam]["Chèque"]["sum"], 3),
                "esp_count": data[cam]["Espèces"]["count"],
                "trt_count": data[cam]["Traite"]["count"],
                "chq_count": data[cam]["Chèque"]["count"],
                "total": round(total, 3),
                "total_count": total_count,
            })

    rows.sort(key=lambda r: r["total"], reverse=True)
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    grand_total = round(sum(r["total"] for r in rows), 3)
    grand_count = sum(r["total_count"] for r in rows)

    return JSONResponse({
        "rows": rows,
        "grand_total": grand_total,
        "grand_count": grand_count,
        "active_cams": len(rows),
        "filename": file.filename,
        "lines_parsed": len(lines) - skipped,
        "lines_skipped": skipped,
    })

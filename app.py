from fastapi import FastAPI, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import re
from collections import defaultdict

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

TARGET_CAMS = {
    "CAM01", "CAM02", "CAM03", "CAM04", "CAM05", "CAM06",
    "CAM36", "CAM37", "CAM38", "CAM48", "CAM49"
}

TYPE_MAP = {
    "CESP":  "Espèces",
    "CTRT":  "Traite",
    "CCHQR": "Chèque",
}

def parse_lines(text: str):
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(";")
        if len(parts) < 11:
            continue

        code = parts[0]          # e.g. CESP-26-03-0001025
        ref2 = parts[1]          # e.g. 26-99-CAM39-00159  (may be empty)
        try:
            amount = float(parts[-1])
        except ValueError:
            continue

        # Determine payment type prefix
        prefix = None
        for p in TYPE_MAP:
            if code.startswith(p):
                prefix = p
                break
        if prefix is None:
            continue

        # Extract CAM from ref2 field
        cam_match = re.search(r"(CAM\d+)", ref2)
        cam = cam_match.group(1) if cam_match else None

        results.append({
            "code": code,
            "cam": cam,
            "type_key": prefix,
            "type_label": TYPE_MAP[prefix],
            "amount": amount,
            "raw": line,
        })
    return results


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    rows = parse_lines(text)

    # ── totals per CAM × type ──────────────────────────────────────────────
    cam_data: dict[str, dict] = defaultdict(lambda: {
        "total": 0.0,
        "count": 0,
        "by_type": defaultdict(lambda: {"amount": 0.0, "count": 0}),
    })

    unmatched_rows = []
    matched_rows   = []

    for r in rows:
        cam = r["cam"]
        if cam in TARGET_CAMS:
            d = cam_data[cam]
            d["total"]  += r["amount"]
            d["count"]  += 1
            t = d["by_type"][r["type_label"]]
            t["amount"] += r["amount"]
            t["count"]  += 1
            matched_rows.append(r)
        else:
            unmatched_rows.append(r)

    # Build ranked list
    ranked = sorted(
        [
            {
                "cam": cam,
                "total": round(v["total"], 3),
                "count": v["count"],
                "by_type": {
                    k: {"amount": round(vt["amount"], 3), "count": vt["count"]}
                    for k, vt in v["by_type"].items()
                },
            }
            for cam, v in cam_data.items()
        ],
        key=lambda x: x["total"],
        reverse=True,
    )

    # Add ranks
    for i, item in enumerate(ranked, 1):
        item["rank"] = i

    # Summary stats
    all_types_summary = defaultdict(lambda: {"amount": 0.0, "count": 0})
    for r in matched_rows:
        all_types_summary[r["type_label"]]["amount"] += r["amount"]
        all_types_summary[r["type_label"]]["count"]  += r["count"] if "count" in r else 1

    return JSONResponse({
        "ranked": ranked,
        "total_rows": len(rows),
        "matched_rows": len(matched_rows),
        "unmatched_rows": len(unmatched_rows),
        "grand_total": round(sum(r["amount"] for r in matched_rows), 3),
        "types_summary": {
            k: {"amount": round(v["amount"], 3), "count": v["count"]}
            for k, v in all_types_summary.items()
        },
        "filename": file.filename,
    })

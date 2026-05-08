from fastapi import FastAPI, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import re
from collections import defaultdict

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

TYPE_MAP = {
    "CESP":  "Espèces",
    "CTRT":  "Traite",
    "CCHQR": "Chèque",
}

CAM_SITE_MAP: dict[str, str] = {
    # SFX
    "CAM01": "SFX", "CAM02": "SFX", "CAM03": "SFX", "CAM04": "SFX",
    "CAM05": "SFX", "CAM06": "SFX", "CAM07": "SFX", "CAM36": "SFX",
    "CAM37": "SFX", "CAM38": "SFX", "CAM48": "SFX", "CAM49": "SFX",
    "CAM58": "SFX", "CAM59": "SFX",
    # MAH
    "CAM40": "MAH", "CAM41": "MAH", "CAM42": "MAH", "CAM43": "MAH",
    "CAM44": "MAH", "CAM45": "MAH", "CAM57": "MAH",
    # NAB
    "CAM50": "NAB", "CAM51": "NAB", "CAM52": "NAB", "CAM53": "NAB",
    "CAM54": "NAB",
    # SSE
    "CAM08": "SSE", "CAM09": "SSE", "CAM10": "SSE", "CAM11": "SSE",
    "CAM12": "SSE", "CAM13": "SSE", "CAM14": "SSE", "CAM15": "SSE",
    "CAM39": "SSE", "CAM46": "SSE", "CAM47": "SSE",
    # TUN
    "CAM16": "TUN", "CAM17": "TUN", "CAM18": "TUN", "CAM19": "TUN",
    "CAM20": "TUN", "CAM21": "TUN", "CAM22": "TUN", "CAM23": "TUN",
    "CAM24": "TUN", "CAM25": "TUN", "CAM26": "TUN", "CAM27": "TUN",
    "CAM29": "TUN", "CAM30": "TUN", "CAM31": "TUN",
}


def get_site(cam: str | None) -> str:
    if cam is None:
        return "Inconnu"
    return CAM_SITE_MAP.get(cam, "Inconnu")

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

    skipped_rows = []
    matched_rows = []

    for r in rows:
        cam = r["cam"]
        if cam is not None:
            d = cam_data[cam]
            d["total"]  += r["amount"]
            d["count"]  += 1
            t = d["by_type"][r["type_label"]]
            t["amount"] += r["amount"]
            t["count"]  += 1
            matched_rows.append(r)
        else:
            skipped_rows.append(r)

    # Build ranked list with flat per-type fields expected by the frontend
    ranked = sorted(
        [
            {
                "cam": cam,
                "site": get_site(cam),
                "total": round(v["total"], 3),
                "total_count": v["count"],
                "esp":       round(v["by_type"].get("Espèces", {}).get("amount", 0.0), 3),
                "trt":       round(v["by_type"].get("Traite",  {}).get("amount", 0.0), 3),
                "chq":       round(v["by_type"].get("Chèque",  {}).get("amount", 0.0), 3),
                "esp_count": v["by_type"].get("Espèces", {}).get("count", 0),
                "trt_count": v["by_type"].get("Traite",  {}).get("count", 0),
                "chq_count": v["by_type"].get("Chèque",  {}).get("count", 0),
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
        all_types_summary[r["type_label"]]["count"]  += 1

    # Sites summary
    sites_acc: dict[str, dict] = defaultdict(lambda: {"amount": 0.0, "count": 0, "cams": set()})
    for cam, v in cam_data.items():
        site = get_site(cam)
        sites_acc[site]["amount"] += v["total"]
        sites_acc[site]["count"]  += v["count"]
        sites_acc[site]["cams"].add(cam)

    sites_summary = {
        k: {
            "amount":    round(v["amount"], 3),
            "count":     v["count"],
            "cam_count": len(v["cams"]),
        }
        for k, v in sorted(sites_acc.items())
    }

    return JSONResponse({
        "rows":         ranked,
        "grand_total":  round(sum(r["amount"] for r in matched_rows), 3),
        "grand_count":  len(matched_rows),
        "lines_parsed": len(rows),
        "active_cams":  len(cam_data),
        "skipped_rows": len(skipped_rows),
        "types_summary": {
            k: {"amount": round(v["amount"], 3), "count": v["count"]}
            for k, v in all_types_summary.items()
        },
        "sites_summary": sites_summary,
        "filename": file.filename,
    })

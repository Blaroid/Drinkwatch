"""Android app distribution: a plain install page and the APK itself.

Sideloading is the honest distribution path for a scraper-backed personal app:
one file, no Play Store review, and no data-safety declaration to get wrong.
"""

from __future__ import annotations

import json
import pathlib

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse

from ..config import settings

router = APIRouter(prefix="/app", tags=["app"])

APK_NAME = "drinkwatch.apk"


def _apk_path() -> pathlib.Path:
    return pathlib.Path(settings.apk_dir) / APK_NAME


def _version() -> dict:
    meta = pathlib.Path(settings.apk_dir) / "version.json"
    if meta.is_file():
        try:
            return json.loads(meta.read_text())
        except ValueError:
            pass
    return {"versionName": "0.1.0", "versionCode": 1, "notes": ""}


@router.get("/version.json", summary="Latest Android build metadata")
def version() -> dict:
    payload = _version()
    payload.setdefault("apk", settings.public_base_url + "/app/" + APK_NAME)
    return payload


@router.get("/" + APK_NAME, summary="Download the Android APK")
def download_apk() -> FileResponse:
    path = _apk_path()
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no APK published yet")
    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        filename=APK_NAME,
        headers={"Cache-Control": "no-cache"},
    )


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def install_page() -> HTMLResponse:
    payload = _version()
    apk_exists = _apk_path().is_file()
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DrinkWatch for Android</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
        margin: 0; padding: 32px 20px; max-width: 640px; }}
 h1 {{ font-size: 28px; margin: 0 0 4px; }}
 p {{ margin: 0 0 14px; }}
 .muted {{ opacity: .7; font-size: 14px; }}
 a.button {{ display: inline-block; padding: 14px 22px; border-radius: 14px;
   background: #137a4b; color: #fff; text-decoration: none; font-weight: 600; }}
 a.button.disabled {{ background: #888; pointer-events: none; }}
 ol {{ padding-left: 20px; }}
 code {{ background: rgba(127,127,127,.15); padding: 2px 6px; border-radius: 6px; }}
</style></head>
<body>
<h1>DrinkWatch</h1>
<p class="muted">Energy drink prices near you, from supermarket sites plus crowd sightings.</p>
<p><a class="button{' disabled' if not apk_exists else ''}" href="/app/{APK_NAME}">Download v{payload.get('versionName', '?')} APK</a></p>
<p class="muted">versionCode {payload.get('versionCode', '?')} · {payload.get('notes', '')}</p>
<h2 style="font-size:18px">Installing</h2>
<ol>
  <li>Download the APK on your phone (Chrome will warn about unknown sources).</li>
  <li>Open the file, allow <em>Install unknown apps</em> for your browser, then install.</li>
  <li>Open DrinkWatch, allow location, and the Prices tab fills in within a few seconds.</li>
</ol>
<p class="muted">After the first install the app updates itself: it checks
<code>/app/version.json</code> on launch, offers the new build in a dialog and hands
the downloaded APK to the system installer. Settings also has a manual check.</p>
<p class="muted">The app stores one anonymous device token (encrypted, on the phone) so crowd
submissions can be attributed and moderated. Photos are stripped of EXIF/GPS on upload.
Store locations come from OpenStreetMap; fuel prices from FuelWatch WA. Supermarket prices
are collected from public product pages — treat them as indicative, not a quote.</p>
</body></html>"""
    return HTMLResponse(body)

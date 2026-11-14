# FIRECTOR — by ASTROARMONIA

Free public satellite-observation research website, with worldwide NASA VIIRS/MODIS detections, provisional Fire Fusion, Sentinel-2 catalogue review and Meteosat WMS availability checks. **Not independently validated; not for emergency dispatch.**

## Deploy for free (one website, frontend and backend)

1. Create a new GitHub repository. Upload the files **inside this directory** to the repository root, including `render.yaml`. Do not upload any `.env` file.
2. Sign up at https://dashboard.render.com/ with GitHub.
3. Choose **New > Blueprint** and select the GitHub repository. The included `render.yaml` configures a **Free** Python Web Service.
4. When requested, enter your **NASA FIRMS MAP_KEY** in the `NASA_FIRMS_KEY` secret field. Never put this key in the HTML, source files, or public GitHub commits.
5. Deploy. When Render says **Live**, open the assigned `https://...onrender.com` website. The dashboard and its `/api/...` endpoints are hosted from the **same website**.
6. Check `/healthz` and `/api/world/summary` on the website. The first NASA worldwide fetch may take some time or fail if NASA rate limits or the key is invalid.

## Important limitations

- The site requires online hosting; opening `index.html` from a file viewer will show data unavailable.
- Free Render services sleep after about 15 minutes idle and may take roughly a minute to wake up. Render free allowances are limited. Not always-on emergency infrastructure.
- VIIRS/MODIS observations are thermal anomalies, not confirmed active wildfires.
- Fire Fusion v6 is **provisional automated candidate grouping** based on sensor footprint/time; Sentinel-2 and Meteosat are supporting catalogue/WMS references, **not automatic verified duplicate removal**.
- The audit file is ephemeral on free hosting; log history may be lost after a restart.
- No `firector.com` domain is included; the Render subdomain is free.

## Run and test locally (optional)

`pip install -r requirements.txt`

`python -m unittest -v test_fusion_v6 test_validation`

`uvicorn world_server:app --port 9100`

Open `http://127.0.0.1:9100`.

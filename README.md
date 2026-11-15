# FIRECTOR — by ASTROARMONIA: Weather Heatmap Update

This package is based on your 15 November uploaded files. It preserves the NASA VIIRS/MODIS observation map, Fire Fusion v6, Sentinel-2 catalogue lookup, Meteosat support, existing high-risk diamond toggle, and the cream-coloured dashboard.

## New feature: optional fire-weather heatmap

The sidebar now includes a **Thermal-style weather overlay** on/off switch. It is **off by default** and works when the map is zoomed to level 5 or higher and the viewport is small enough. It shades locations across the **visible map** with hot, dry and windy weather conditions using Open-Meteo data, including places where NASA detected **no fires**. Colours indicate an **illustrative weather screening score**, not an ignition probability, measured thermal surface temperatures, vegetation dryness, an official FWI, or a safety assessment. Missing weather is not treated as safe weather.

The old **Show high-risk areas** diamonds remain as a separate experimental hotspot-plus-weather switch. The new heatmap does not change Fire Fusion or duplicate matching. The new endpoint is `/api/world/weather-heat` and requires no extra key; it uses Open-Meteo's free non-commercial weather API, with attribution and caching. Terms: https://open-meteo.com/en/terms .

## Update your existing local VS Code project

1. Back up `index.html` and `world_server.py` in your `FIRECTOR` folder.
2. From this package, replace **only** `index.html` and `world_server.py` in that folder. Leave your existing `.env` and its NASA key untouched.
3. Open the VS Code terminal inside that folder, activate your existing `.venv` if required, and run:

   ```sh
   python3 -m uvicorn world_server:app --port 9100
   ```

4. Open `http://127.0.0.1:9100` and turn on **Thermal-style weather overlay** after zooming in to your region. If port 9100 is occupied, stop the old Uvicorn process or use a free port.

If your local folder is different, don't type `cd ~/Desktop/FIRECTOR`; start from the folder where `world_server.py` lives.

## Update the public website

The public website is **not updated merely by downloading this package**. To update the Render website, replace `index.html` and `world_server.py` in your GitHub repository, commit the changes, and allow Render's automatic deployment to complete. Do not upload `.env` to GitHub.

## Testing

```sh
python3 -m unittest -v test_fusion_v6 test_validation test_risk test_meteosat test_weather_heat
```

These are only software/synthetic checks. Live Open-Meteo availability, browser rendering on your device, model calibration and independent fire-danger validation have **not** been established. Use official authority fire danger maps and advisories for decisions about safety or dispatch. Satellite hotspots are not necessarily wildfires.

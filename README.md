# Transmission Flight Path Planner

Standalone Python-generated mapping prototype for planning conceptual helicopter inspection routes around transmission infrastructure.

The interactive app supports three study areas:

- Montreal / YUL
- Nipawin / CYBU
- Lac La Ronge / CYVC

It combines transmission data, mapped support/tower points, OpenStreetMap context layers, an Airbus H125 fuel model, hover time per stop, multiple helicopters, multiple refuel-and-return sorties, lasso planning areas, and fuel-limited coverage warnings.

> This is a GIS planning prototype, not an aviation flight-planning or dispatch system. It does not replace approved operational procedures, current NOTAMs, airspace data, weather, obstacle/terrain review, company manuals, fuel calculations, or ATC clearance.

## Requirements

- Python 3.10 or newer
- `Pillow` for static map image generation
- A modern browser such as Chrome, Firefox, Safari, or Edge

The core scripts otherwise use Python's standard library. Internet access is only needed when downloading missing source data, refreshing OpenStreetMap context layers, or loading online map tiles/Leaflet assets in the browser.

## First-time setup

From the project folder:

```bash
cd /Users/micyoung/Documents/Utilities_Samples
python3 -m pip install Pillow
```

If your system Python is protected, create and use a virtual environment instead:

```bash
cd /Users/micyoung/Documents/Utilities_Samples
python3 -m venv .venv
source .venv/bin/activate
python -m pip install Pillow
```

## Generate the app

Run the visualizer whenever `scripts/visualize_transmission_lines.py` changes or source/context data is refreshed:

```bash
cd /Users/micyoung/Documents/Utilities_Samples
python3 scripts/visualize_transmission_lines.py
```

The generator writes these artifacts to `outputs/`:

- `interactive_flight_path_picker.html`: interactive map application
- `montreal_transmission_lines.png` and `.html`: static Montreal overview
- `yul_to_transmission_midpoint_flight_path.png` and `.html`: static conceptual route example

If the Montreal GeoPackage is missing, the generator downloads it automatically into `data/`.

## Run the interactive app locally

Serve the `outputs/` folder over HTTP. Do not rely on opening the HTML directly with `file://`, because browser security/caching can interfere with tiles and script loading.

```bash
cd /Users/micyoung/Documents/Utilities_Samples
python3 -m http.server 8768 --directory outputs
```

Open this URL in a browser:

```text
http://localhost:8768/interactive_flight_path_picker.html
```

Keep the terminal running while using the app. Stop the server with `Control-C` in that terminal.

### If port 8768 is already in use

Choose another port, for example:

```bash
python3 -m http.server 8769 --directory outputs
```

Then open:

```text
http://localhost:8769/interactive_flight_path_picker.html
```

### Refreshing after code changes

1. Stop the current local server with `Control-C` if needed.
2. Re-run the generator:

   ```bash
   python3 scripts/visualize_transmission_lines.py
   ```

3. Start the server again.
4. Refresh the browser page. Use a hard reload if the old page is still displayed:
   - macOS Chrome: `Command-Shift-R`
   - Windows/Linux Chrome: `Control-Shift-R`

## Using the planner

### Select a study area

Choose `Montreal / YUL`, `Nipawin / CYBU`, or `Lac La Ronge / CYVC` from **Study area**. This resets the route and loads the area-specific transmission, tower/support, airport, forest, water, road, and simplified airspace-reference data.

### Manual route planning

Click a transmission cable segment to add a snapped stop. The planner reorders stops to minimize conceptual horizontal travel distance. Click a stop label on the map to remove it.

For manual fleet planning:

1. Set the number of helicopters under **Fleet inspection planning**.
2. Enable **Allow multi-helicopter planning**.
3. Add cable stops.

Each helicopter uses a distinct route color. The route legend below the readout explains the color, stop count, sortie count, distance, and hover time for each aircraft.

### Tower coverage planning

1. Set **Helicopters** from 1 to 4.
2. Set **Tower coverage target** from 5% to 100%.
3. Leave **Use all selected helicopters** enabled when each selected aircraft should receive work. Disable it to let the optimizer use fewer aircraft when that is cheaper.
4. Click **Plan tower coverage**.

The planner chooses support/tower points, accounts for return-to-base fuel reserve and five minutes of hover time per stop, and creates additional refuel-and-return sorties as needed.

The planning status updates during calculation. On a large 100% coverage request, wait for the status to progress through tower selection and sortie polishing.

### Scope coverage with multiple lassos

Use lassos to plan only selected corridor sections:

1. Click **Draw lasso**.
2. Drag around one corridor section on the map.
3. Click **Draw lasso** again and drag around another section.
4. Repeat for additional sections.
5. Click **Plan tower coverage**.

All lasso polygons are retained until **Clear lasso** is clicked. The planning scope is the union of towers/supports inside any lasso. Teal polygons show the lasso areas, and teal markers show the in-scope tower/support points.

### Fuel-limited plans

The prototype models:

- Travel fuel using an Airbus H125 range-derived model
- A 30-minute reserve
- Five minutes of hover per stop
- Hover burn at 1.15 times the endurance-derived burn rate

When the requested target cannot be met under these assumptions, the status becomes **Fuel-limited coverage** and red markers identify the tower/support points that were not included. The map layer control and legend can also show or hide those markers.

## Refreshing OpenStreetMap context layers

`scripts/fetch_context_layers.py` downloads prototype context data from OpenStreetMap through Overpass and writes GeoJSON under `data/processed/<area>/`.

Refresh all available layers for all areas:

```bash
cd /Users/micyoung/Documents/Utilities_Samples
python3 scripts/fetch_context_layers.py
python3 scripts/visualize_transmission_lines.py
```

Refresh a single area:

```bash
python3 scripts/fetch_context_layers.py --area laronge
python3 scripts/visualize_transmission_lines.py
```

Refresh selected layers only:

```bash
python3 scripts/fetch_context_layers.py --area nipawin --layer lines --layer structures --layer water
python3 scripts/visualize_transmission_lines.py
```

The Overpass API can be slow or rate-limited. The script retries its configured endpoints, but a retry later may still be necessary.

## Project layout

```text
data/
  lignes-transport-electrique-2020.gpkg    Montreal source GeoPackage
  processed/                               Area-specific GeoJSON context layers
  osm_tiles/                               Cached OpenStreetMap tiles for static images
outputs/
  interactive_flight_path_picker.html      Generated interactive application
scripts/
  visualize_transmission_lines.py          Main generator and client-side planner source
  fetch_context_layers.py                  Optional Overpass/OSM data refresh script
```

## Troubleshooting

### `ModuleNotFoundError: No module named 'PIL'`

Install Pillow:

```bash
python3 -m pip install Pillow
```

### The app looks unchanged after regeneration

Confirm the generator completed, then hard-refresh the browser. Make sure the server is serving the `outputs/` folder from this project directory.

### Map tiles are blank

Verify internet access. The interactive map uses OpenStreetMap and Esri imagery tiles. You can still inspect local data layers, but the basemap may be unavailable offline.

### Coverage planning is taking a while

Large scope + high coverage means more tower/support points and more sorties to evaluate. The planner shows progress beneath the planning button. Start with a lasso, a smaller coverage percentage, or fewer areas to validate the workflow before requesting a full-area plan.

### 100% coverage shows red tower markers

The target is not feasible under the current aircraft count, travel distance, five-minute hover-per-stop allowance, and reserve requirement. The red markers identify excluded towers/supports. Increase available aircraft, reduce the lasso scope/coverage target, or revisit the fuel and operating assumptions.

## Data and operating caveats

- Montreal transmission data originates from the City of Montreal GeoPackage download.
- Northern Saskatchewan and context layers are OpenStreetMap/Overpass prototype extracts.
- Displayed airspace rings are simplified reference aids, not authoritative controlled-airspace geometry.
- Route geometry is conceptual straight-line planning; it does not account for terrain, obstacles, weather, winds, traffic, payload, route approvals, operating limits, or real dispatch/fuel planning.

# Crime Near Me – history data

Builds the downloadable historical crime packs for the Crime Near Me app.
Source: data.police.uk archives (street-level crime, December 2010 onwards).
Contains public sector information licensed under the Open Government Licence v3.0.

## Set up (once)
1. Create a **public** GitHub repo called `crime-near-me-data`. It must be public so the app can download the files.
2. Upload these files, keeping the `.github/workflows` folder.

## Build the packs
1. Open the repo on GitHub, then **Actions**, then **Build crime history packs**, then **Run workflow**.
2. To test it quickly, type `north-wales` in the box. Leave it blank to build every force (this takes longer).
3. When it finishes, a new **Release** appears with one file per force plus `manifest.json`.
   The app always reads the latest release:
   `https://github.com/YOUR_GITHUB_USERNAME/crime-near-me-data/releases/latest/download/manifest.json`

Run it again whenever you want to refresh the data (once or twice a year is plenty).

## Pack format
Each `<force>.sqlite.deflate` is a SQLite database compressed with raw DEFLATE.
- `crimes(month, cat, lat, lng, street, outcome)`: month as YYYYMM; lat/lng × 100000
- `cats`, `streets`, `outcomes`: lookup tables
- `meta`: force, name, from, to, crimes, months, bounding box, version

# Injectable-Combination-Trials

DDCP-related products under clinical trial: ongoing Phase 2 and 3 parenteral trials that use a drug-device combination (autoinjector, pen, prefilled syringe, on-body injector and similar), from ClinicalTrials.gov and the EU Clinical Trials Information System, with published revenue figures for industry Phase 3 assets.

Live page: https://sushilkk81.github.io/Injectable-Combination-Trials/

## How it stays current

`.github/workflows/refresh.yml` runs every Monday at 09:00 IST (and whenever the files below change). It runs `scripts/build.py`, which re-queries both registries, rebuilds `index.html` and `data/trials.json`, and commits them. GitHub Pages then republishes the site. You can also start a refresh by hand from the **Actions** tab → *Refresh trial data* → *Run workflow*.

| File | What it controls |
|---|---|
| `data/curation.json` | Trials excluded after manual review, and device-class overrides |
| `data/revenue.json` | Revenue figures per asset, with source and date (edited by hand) |
| `data/config.json` | US / Europe share used to split worldwide revenue when the company does not report regions |
| `site/template.html` | Page layout |
| `data/build-log.txt` | Counts and any errors from the latest refresh |

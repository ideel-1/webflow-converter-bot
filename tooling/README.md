# Post-export toolkit (Webflow/Exflow → Cloudflare Pages)

## Install
- Python 3.9+
- `pip install -r tooling/requirements.txt`

## Run
```bash
python tooling/postexport.py --input PATH/TO/export.zip --config tooling/config.example.json
```
Or if you exported to a folder:
```bash
python tooling/postexport.py --input PATH/TO/export-folder --config tooling/config.example.json
```

## What it does
- Fixes extensionless HTML filenames and rewires internal links.
- Optionally downloads external images and builds responsive WebP variants, updating `<img>` tags with `srcset`.
- Ensures `index.html` exists.
- Formats HTML/CSS/JS for readability.
- Generates `sitemap.xml`.

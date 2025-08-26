#!/usr/bin/env python3
import os, re, io, json, shutil, zipfile
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from PIL import Image
import requests

CONFIG_DEFAULT = {
    "site_url": "https://example.com",
    "output_dir": "dist",
    "preferred_homepage": "",
    "download_images": True,
    "image_dir": "images",
    "image_variants": [480, 800, 1200, 1600, 2000],
    "image_format": "webp",
    "quality": 82,
    "format_html": True,
    "format_css_js": True,
    "create_sitemap": True
}

def load_config(path):
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        x = CONFIG_DEFAULT.copy(); x.update(cfg); return x
    return CONFIG_DEFAULT

def ensure_dir(p): os.makedirs(p, exist_ok=True)

def copy_src(src, outdir):
    if src.lower().endswith(".zip") and os.path.isfile(src):
        with zipfile.ZipFile(src, "r") as z: z.extractall(outdir)
    else:
        if os.path.exists(outdir): shutil.rmtree(outdir)
        shutil.copytree(src, outdir)

def looks_like_html(path):
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", errors="ignore").lower()
        return ("<!doctype html" in head) or ("<html" in head and "</html>" in head)
    except Exception:
        return False

def find_and_fix_extensionless_html(dist_root):
    renamed = {}
    for dirpath, _, filenames in os.walk(dist_root):
        for name in filenames:
            if "." in name: continue
            full = os.path.join(dirpath, name)
            if looks_like_html(full):
                new_full = full + ".html"
                os.rename(full, new_full)
                rel_old = os.path.relpath(full, dist_root).replace(os.sep, "/")
                rel_new = os.path.relpath(new_full, dist_root).replace(os.sep, "/")
                renamed[rel_old] = rel_new
    return renamed

ATTR_PATTERN = re.compile(r'(?P<attr>\b(?:href|src|action)\s*=\s*["\'])(?P<val>[^"\']+)(["\'])', re.IGNORECASE)

def rewrite_links_in_html(file_path, renamed_map, dist_root):
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    def fix_link(val):
        if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', val): return val
        if val.startswith("#") or val.startswith("data:"): return val
        norm = val
        while norm.startswith("./"): norm = norm[2:]
        norm = norm.lstrip("/")
        base = norm.split("#",1)[0].split("?",1)[0]
        if base in renamed_map:
            prefix = "/" if val.startswith("/") else ""
            q = ""; h = ""
            qpos = val.find("?"); hpos = val.find("#")
            if qpos != -1 and (hpos == -1 or qpos < hpos): q = val[qpos:(hpos if hpos!=-1 else len(val))]
            if hpos != -1: h = val[hpos:]
            return prefix + renamed_map[base] + q + h
        return val
    def repl(m):
        before = m.group("val"); after = fix_link(before)
        return m.group("attr") + after + m.group(3)
    new_text = ATTR_PATTERN.sub(repl, text)
    if new_text != text:
        with open(file_path, "w", encoding="utf-8") as f: f.write(new_text)

def hash12(b):
    import hashlib; return hashlib.sha1(b).hexdigest()[:12]

def filename_from_url(url):
    from urllib.parse import urlparse; import os
    p = urlparse(url); name = os.path.basename(p.path)
    return name or "asset"

def download_image(url, dest_dir):
    try:
        import requests
        r = requests.get(url, timeout=30); r.raise_for_status()
        data = r.content; ensure_dir(dest_dir)
        base = filename_from_url(url); h = hash12(data); name = f"{h}-{base}"
        out = os.path.join(dest_dir, name)
        with open(out, "wb") as f: f.write(data)
        return out
    except Exception as e:
        print(f"[warn] download failed {url}: {e}"); return None

def make_variants(img_path, out_dir, widths, fmt="webp", quality=82):
    ensure_dir(out_dir); variants = []
    try:
        from PIL import Image
        im = Image.open(img_path); w, h = im.size
        for target in widths:
            if target >= w: continue
            ratio = target/float(w); size = (target, max(1, int(h*ratio)))
            im_resized = im.resize(size, Image.LANCZOS)
            stem = os.path.splitext(os.path.basename(img_path))[0]
            ext = fmt if fmt != "keep" else os.path.splitext(img_path)[1].lstrip(".")
            out_name = f"{stem}-{target}w.{ext}"; out_path = os.path.join(out_dir, out_name)
            save_kwargs = {}
            if fmt in ("webp","jpeg","jpg","png"): save_kwargs["quality"] = quality
            im_resized.save(out_path, format=ext.upper(), **save_kwargs)
            variants.append((target, out_path))
        # largest
        stem = os.path.splitext(os.path.basename(img_path))[0]
        ext = fmt if fmt != "keep" else os.path.splitext(img_path)[1].lstrip(".")
        largest_name = f"{stem}-{w}w.{ext}"; largest_path = os.path.join(out_dir, largest_name)
        if fmt == "keep":
            import shutil; shutil.copy2(img_path, largest_path)
        else:
            im.save(largest_path, format=ext.upper(), quality=quality)
        variants.append((w, largest_path))
    except Exception as e:
        print(f"[warn] variants failed {img_path}: {e}")
    variants.sort(key=lambda t: t[0]); return variants

def rewrite_img_tags(file_path, cfg, dist_root):
    import json, os
    from bs4 import BeautifulSoup

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()
    soup = BeautifulSoup(html, "lxml")

    changed = False
    img_dir = os.path.join(dist_root, cfg["image_dir"])
    orig_dir = os.path.join(img_dir, "original")
    var_dir  = os.path.join(img_dir, "responsive")
    cache_path = os.path.join(img_dir, "_cache.json")

    # Load cache: maps original URL -> { "src": "/images/responsive/..", "sizes": "...", "variants": [{"w":1200,"path":"/images/responsive/..."}] }
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as cf:
                cache = json.load(cf)
        except Exception:
            cache = {}

    def variants_exist(entry):
        # ensure all referenced files exist on disk
        paths = [entry.get("src", "")]
        paths += [v.get("path", "") for v in entry.get("variants", [])]
        for p in paths:
            if not p:
                return False
            local = os.path.join(dist_root, p.lstrip("/"))
            if not os.path.exists(local):
                return False
        return True

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue

        # normalize protocol-relative
        if src.startswith("//"):
            src = "https:" + src

        # Only handle external images when download_images is True
        if not cfg.get("download_images", True):
            continue
        if not (src.startswith("http://") or src.startswith("https://")):
            # already local; leave it
            continue

        # FAST PATH: reuse from cache if available and files still exist
        if cfg.get("reuse_cache", True) and src in cache and variants_exist(cache[src]):
            entry = cache[src]
            img["src"] = entry["src"]
            img["srcset"] = ", ".join(f'{v["path"]} {v["w"]}w' for v in entry.get("variants", []))
            if not img.get("sizes"):
                img["sizes"] = entry.get("sizes", "(max-width: 1200px) 100vw, 1200px")
            changed = True
            continue

        # Otherwise: download and generate as before
        downloaded = download_image(src, orig_dir)
        if not downloaded:
            continue

        variants = make_variants(
            downloaded,
            var_dir,
            cfg["image_variants"],
            cfg["image_format"],
            cfg["quality"]
        )
        if not variants:
            continue

        # Choose mid-size as default src
        mid_idx = min(len(variants)//2, len(variants)-1)
        mid_path = variants[mid_idx][1]
        srcset_parts = []
        variant_entries = []
        for w, p in variants:
            rel = os.path.relpath(p, dist_root).replace(os.sep, "/")
            srcset_parts.append(f"/{rel} {w}w")
            variant_entries.append({"w": w, "path": f"/{rel}"})

        local_src = "/" + os.path.relpath(mid_path, dist_root).replace(os.sep, "/")
        img["src"] = local_src
        img["srcset"] = ", ".join(srcset_parts)
        if not img.get("sizes"):
            img["sizes"] = "(max-width: 1200px) 100vw, 1200px"
        changed = True

        # Update cache
        cache[src] = {
            "src": local_src,
            "sizes": img.get("sizes"),
            "variants": variant_entries
        }

    if changed:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(soup.prettify())
        # persist cache to disk
        try:
            os.makedirs(img_dir, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as cf:
                json.dump(cache, cf, indent=2)
        except Exception:
            pass


def ensure_index(dist_root, preferred=""):
    index_path = os.path.join(dist_root, "index.html")
    if os.path.exists(index_path): return
    all_html = []
    for dirpath, _, filenames in os.walk(dist_root):
        for n in filenames:
            if n.lower().endswith(".html"):
                rel = os.path.relpath(os.path.join(dirpath, n), dist_root).replace(os.sep, "/")
                all_html.append(rel)
    cand = ""
    if preferred and any(os.path.basename(p) == preferred for p in all_html):
        cand = next(p for p in all_html if os.path.basename(p) == preferred)
    elif all_html:
        cand = all_html[0]
    meta = f'<meta http-equiv="refresh" content="0; url=/{cand}">' if cand else ""
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'>{meta}<title>Index</title></head><body><p>Loading… <a href='/{cand}'>Continue</a></p></body></html>")

def format_code(dist_root, format_html=True, format_css_js=True):
    import re
    from bs4 import BeautifulSoup
    def brace_format(text):
        s = text.replace("{","{\n").replace("}","\n}\n").replace(";",";\n")
        s = re.sub(r"\n{3,}", "\n\n", s)
        indent = 0; out=[]
        for raw in s.splitlines():
            line = raw.strip()
            if line.startswith("}"): indent = max(indent-1,0)
            out.append(("  "*indent)+line)
            if line.endswith("{"): indent += 1
        return "\n".join(out).strip()+"\n"
    for dirpath, _, filenames in os.walk(dist_root):
        for n in filenames:
            p = os.path.join(dirpath, n); low = n.lower()
            try:
                if format_html and low.endswith(".html"):
                    with open(p,"r",encoding="utf-8",errors="ignore") as f:
                        soup = BeautifulSoup(f.read(),"html.parser")
                    with open(p,"w",encoding="utf-8") as f: f.write(soup.prettify())
                elif format_css_js and (low.endswith(".css") or low.endswith(".js")):
                    with open(p,"r",encoding="utf-8",errors="ignore") as f: txt=f.read()
                    with open(p,"w",encoding="utf-8") as f: f.write(brace_format(txt))
            except Exception as e:
                print(f"[warn] format failed {p}: {e}")

def write_sitemap(dist_root, site_url):
    urls = []
    for dirpath, _, filenames in os.walk(dist_root):
        for n in filenames:
            if n.lower().endswith(".html"):
                rel = os.path.relpath(os.path.join(dirpath, n), dist_root).replace(os.sep, "/")
                urls.append(rel)
    xml = ["<?xml version='1.0' encoding='UTF-8'?>","<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"]
    for u in sorted(urls): xml.append(f"  <url><loc>{site_url.rstrip('/')}/{u}</loc></url>")
    xml.append("</urlset>")
    with open(os.path.join(dist_root,"sitemap.xml"),"w",encoding="utf-8") as f: f.write("\n".join(xml))

def main():
    import argparse, os
    ap = argparse.ArgumentParser(description="Post-export pipeline for Webflow/Exflow → Cloudflare Pages")
    ap.add_argument("--input", required=True, help="Path to export (folder or .zip)")
    ap.add_argument("--config", default="", help="Path to config JSON")
    ap.add_argument("--output", default="", help="Override output directory")
    args = ap.parse_args()
    cfg = load_config(args.config)
    dist_root = os.path.abspath(args.output or cfg["output_dir"])
    src = os.path.abspath(args.input)

    print("▶ Copying source…"); copy_src(src, dist_root)
    print("▶ Fixing extensionless HTML…"); renamed = find_and_fix_extensionless_html(dist_root)
    print(f"  - Renamed {len(renamed)} files") if renamed else None
    print("▶ Rewriting intra-site links…")
    for dirpath, _, filenames in os.walk(dist_root):
        for n in filenames:
            if n.lower().endswith(".html"):
                rewrite_links_in_html(os.path.join(dirpath,n), renamed, dist_root)
    print("▶ Ensuring index.html…"); ensure_index(dist_root, cfg.get("preferred_homepage",""))
    if cfg.get("download_images", True):
        print("▶ Downloading images and building responsive variants…")
        for dirpath, _, filenames in os.walk(dist_root):
            for n in filenames:
                if n.lower().endswith(".html"):
                    rewrite_img_tags(os.path.join(dirpath,n), cfg, dist_root)
    if cfg.get("format_html", True) or cfg.get("format_css_js", True):
        print("▶ Formatting HTML/CSS/JS…")
        format_code(dist_root, cfg.get("format_html", True), cfg.get("format_css_js", True))
    if cfg.get("create_sitemap", True):
        print("▶ Writing sitemap.xml…")
        write_sitemap(dist_root, cfg.get("site_url","https://example.com"))
    print(f"✅ Done. Output at: {dist_root}")

if __name__ == "__main__":
    main()

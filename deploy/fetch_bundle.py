"""Put the data bundle at /data/bundle: from the build context if it was copied
in (local builds), otherwise from the public dataset repo (Spaces builds)."""
import shutil, sys
from pathlib import Path

# Bump when the dataset changes: the Space caches this build layer on the
# script's content, so an unchanged script means an unchanged bundle.
BUNDLE_VERSION = "2026-09-15b"

dst = Path("/data/bundle")
local = Path("/app/deploy/bundle")
if local.exists() and any(local.iterdir()):
    shutil.copytree(local, dst, dirs_exist_ok=True)
    print("  bundle from build context", flush=True)
else:
    from huggingface_hub import snapshot_download
    snapshot_download("bolat-t/kgrag-data", repo_type="dataset", local_dir=str(dst))
    print("  bundle from bolat-t/kgrag-data", flush=True)
print(sorted(p.name for p in dst.rglob("*") if p.is_file()), flush=True)

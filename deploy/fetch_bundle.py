"""Put the data bundle at /data/bundle: from the build context if it was copied
in (local builds), otherwise from the public dataset repo (Spaces builds)."""
import shutil, sys
from pathlib import Path

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

#!/usr/bin/env bash
# Provision a fresh Ubuntu 24.04 (arm64) VM into a running demo.
# Run as the default user over ssh:  bash setup.sh
set -euo pipefail

PUBLIC_IP=$(curl -s https://api.ipify.org)
HOST="${PUBLIC_IP//./-}.sslip.io"     # free DNS: 1-2-3-4.sslip.io -> 1.2.3.4
echo "== public host: $HOST"

# Oracle's Ubuntu images ship iptables rules that drop everything but 22.
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save >/dev/null 2>&1 || true

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

sudo mkdir -p /srv/kgrag && sudo chown "$USER" /srv/kgrag
cd /srv/kgrag
if [ ! -d repo ]; then git clone --depth 1 https://github.com/bolat-t/knowledge-graph-rag repo; else git -C repo pull -q; fi

# Build on the box: the data bundle comes from the public HF dataset.
sudo docker build -t kgrag-space repo

cat > Caddyfile <<CADDY
$HOST {
    reverse_proxy kgrag:7860
    encode zstd gzip
}
CADDY

sudo docker network create kgrag-net 2>/dev/null || true
sudo docker rm -f kgrag caddy 2>/dev/null || true
sudo docker run -d --name kgrag --network kgrag-net --restart unless-stopped \
  --memory 10g kgrag-space
sudo docker run -d --name caddy --network kgrag-net --restart unless-stopped \
  -p 80:80 -p 443:443 -v /srv/kgrag/Caddyfile:/etc/caddy/Caddyfile \
  -v caddy_data:/data caddy:2

echo "== waiting for the app"
until curl -sf -o /dev/null http://localhost:7860/api/health 2>/dev/null \
   || sudo docker exec kgrag curl -sf -o /dev/null http://127.0.0.1:7860/api/health; do sleep 5; done
echo "== live at https://$HOST"

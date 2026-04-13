#!/bin/bash
set -e

echo "========== AlgoX Setup Starting =========="

# Install system packages
apt update && apt install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx screen

# Setup Python environment
cd /root/delta_neutral
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create systemd service
cat > /etc/systemd/system/algox.service << 'EOF'
[Unit]
Description=AlgoX Delta Neutral Bot
After=network.target

[Service]
User=root
WorkingDirectory=/root/delta_neutral
EnvironmentFile=/root/delta_neutral/.env
ExecStart=/root/delta_neutral/venv/bin/gunicorn --bind 127.0.0.1:5000 --workers 2 --threads 4 --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now algox

# Setup Nginx
cat > /etc/nginx/sites-available/algox << 'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name algox.co.in;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 120s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/algox /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
mkdir -p /var/www/html
nginx -t && systemctl restart nginx

# SSL
echo ""
echo "========== Getting SSL Certificate =========="
certbot --nginx -d algox.co.in --non-interactive --agree-tos --register-unsafely-without-email || echo "⚠️  SSL failed - make sure DNS A record points to this server. Run 'certbot --nginx -d algox.co.in' manually later."

echo ""
echo "========== Setup Complete =========="
echo "App status:"
systemctl status algox --no-pager
echo ""
echo "Visit: https://algox.co.in"

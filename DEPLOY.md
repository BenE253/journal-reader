# Deploying PaperShelf to Oracle Cloud (Always Free)

This guide gets PaperShelf running on Oracle Cloud's **Always Free** tier —
an ARM server with enough RAM (up to 24 GB) to run Marker conversions,
at $0/month — reachable from your iPhone anywhere via Tailscale.

Time required: roughly an hour, most of it waiting on installs.

---

## 1. Create the Oracle Cloud account

1. Sign up at <https://www.oracle.com/cloud/free/>. A credit card is
   required for identity verification, but Always Free resources never
   charge it.
2. **Choose your home region carefully — it cannot be changed later.**
   Pick one geographically close to you (e.g. `US Midwest (Chicago)` or
   `US East (Ashburn)`). ARM capacity varies by region.
3. After signup, consider upgrading the account to **Pay As You Go**
   (Billing → Upgrade). Counterintuitively, this is the reliability move:
   Always Free resources still cost $0, but PAYG accounts get priority
   ARM capacity and — importantly — Oracle's policy of **reclaiming idle
   Always Free instances** does not apply to PAYG accounts. If you stay
   on the free account tier, log into the app regularly; an instance
   idle for a week can be stopped by Oracle.

## 2. Create the server

Console → Compute → Instances → **Create instance**:

- **Image:** Ubuntu 24.04 (choose the *aarch64/ARM* build)
- **Shape:** `VM.Standard.A1.Flex` — set **4 OCPUs and 24 GB RAM**
  (the maximum Always Free allocation; there's no reason to take less)
- **Boot volume:** bump to 100–200 GB (Always Free includes 200 GB total)
- **SSH key:** paste your public key. If you've never made one, on your
  Mac run `ssh-keygen -t ed25519` and paste the contents of
  `~/.ssh/id_ed25519.pub`
- Leave the default networking (VCN) as created. **Do not add any
  ingress rules for port 8000** — the app will only ever be reached
  over Tailscale, which needs no open ports.

If creation fails with **"Out of capacity"** (common for ARM shapes):
try again at a different time of day, try another availability domain,
or upgrade to PAYG (see step 1) which usually resolves it. Persistence
wins here.

Note the instance's **public IP** once it's running.

## 3. First login and setup

From your Mac:

```bash
ssh ubuntu@<PUBLIC_IP>
```

Then, on the server:

```bash
# Basics
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip git

# Tailscale — this is the app's security layer; the app itself has no login
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# Follow the printed URL to authorize the server into your tailnet.
```

After `tailscale up`, the server appears in your Tailscale admin console
(<https://login.tailscale.com/admin/machines>) with a `100.x.y.z`
address and a name like `papershelf` (you can rename it there). From now
on you can also SSH over Tailscale and ignore the public IP entirely.

## 4. Install PaperShelf

```bash
git clone https://github.com/BenE253/journal-reader.git
cd journal-reader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # large: PyTorch etc. Takes a while.
```

## 5. Bring your existing library over

Everything PaperShelf knows — database, PDFs, converted papers, figures,
highlights — lives in the `storage/` folder. Copy it from your Mac
(over Tailscale, so both machines just need Tailscale on):

```bash
# On your MAC, from ~/Code/Projects/journal-reader:
scp -r storage/ ubuntu@<SERVER-TAILSCALE-IP>:~/journal-reader/
```

Skip this if you're happy starting with an empty library.

## 6. Run it permanently (systemd)

Create the service file:

```bash
sudo tee /etc/systemd/system/papershelf.service > /dev/null <<'EOF'
[Unit]
Description=PaperShelf paper reader
After=network-online.target tailscaled.service

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/journal-reader
ExecStart=/home/ubuntu/journal-reader/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now papershelf
```

Check it: `systemctl status papershelf` should say **active (running)**,
and `curl http://localhost:8000` should return HTML. The service starts
on boot and restarts itself if it crashes.

To update the app later:

```bash
cd ~/journal-reader && git pull && sudo systemctl restart papershelf
```

## 7. iPhone setup

1. Install the **Tailscale** app from the App Store, sign into the same
   account, and flip the VPN toggle on. (Leave "VPN On-Demand" enabled
   so it reconnects automatically.)
2. In Safari, open `http://<server-tailscale-ip>:8000` — or the machine
   name, e.g. `http://papershelf:8000`, if MagicDNS is on (it is by
   default).
3. Share button → **Add to Home Screen**.

That's it: papers upload, convert, and read from anywhere your phone has
signal. Your Mac no longer needs to be involved or awake.

## 8. Back up your library

`storage/` **is** your library; everything else is reproducible from the
repo. A nightly copy back to your Mac (run on the Mac, e.g. via a cron
job or just occasionally by hand):

```bash
rsync -az ubuntu@<SERVER-TAILSCALE-IP>:~/journal-reader/storage/ \
      ~/PaperShelfBackup/storage/
```

## Notes & troubleshooting

- **First conversion is slow:** Marker downloads ~3 GB of models the
  first time a PDF is converted. Later conversions take a few minutes
  each on this CPU — they run in the background, so this rarely matters.
- **Never expose port 8000 publicly.** The app has no password; Tailscale
  is the lock on the door. Oracle's default security list already blocks
  it — leave it that way.
- **"Out of capacity" at instance creation:** see step 2; retry or PAYG.
- **Instance stopped by Oracle:** free-tier idle reclamation (see step 1).
  Start it again from the console; upgrade to PAYG to prevent recurrence.
- **Ubuntu on Oracle images ship restrictive iptables rules.** These
  don't affect Tailscale traffic, so PaperShelf works without touching
  them — that's a feature, not a bug.

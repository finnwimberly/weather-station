# Weather Station — Setup Guide

For a **fresh Raspberry Pi 3 Model B** + **Inky Impression 7.3" (2025 edition)** and a blank microSD card. Work through the phases in order. Each phase ends with a check so you know it worked before moving on.

There's no soldering and no loose wiring — the Inky is a HAT that plugs straight onto the Pi's 40‑pin header. "Connecting components" here is really: flash the card, seat the display, then configure software.

---

## Phase 1 — Flash the microSD card (on your computer)

You do this on your laptop/desktop, not the Pi.

1. Download and install the **Raspberry Pi Imager** from https://www.raspberrypi.com/software/ (Windows/macOS/Linux).
2. Put the microSD card into your computer (use an adapter if needed).
3. Open Imager and choose:
   - **Device:** Raspberry Pi 3
   - **Operating System:** *Raspberry Pi OS (64-bit)* — the full version with desktop is fine for a first build. (Lite works too and is lighter, but desktop is friendlier if you ever plug in a monitor.)
   - **Storage:** your microSD card
4. Click **Next**, then **Edit Settings** (the customization screen). Set these so the Pi is reachable without a monitor:
   - **Hostname:** `weatherpi`
   - **Username:** `fiwi42`  &nbsp;**Password:** *(pick one you'll remember)*
   - **Configure wireless LAN:** your Wi‑Fi name + password + your country
   - **Locale / timezone:** your timezone
   - On the **Services** tab: tick **Enable SSH** → *Use password authentication*
5. Save, then **Write**. This erases the card and takes a few minutes. When it finishes, eject the card.

> Note the username matters: the auto‑start service files are set to `fiwi42`. If you ever reflash with a different name, you'll need to update the two service files in `deploy/`.

**Check:** Imager reports "Write Successful."

---

## Phase 2 — Attach the display and power on

Do this with the Pi **unplugged**.

1. Line up the **Inky Impression** so its connector covers all 40 pins of the Pi's GPIO header, and press it down firmly and evenly. It only fits one way; the board sits over the Pi.
2. Insert the flashed microSD card into the Pi's card slot.
3. Plug in the power supply (a proper 5V/2.5A micro‑USB supply for the Pi 3).

The Pi boots. The first boot takes a couple of minutes and the e‑paper screen stays blank for now — that's expected; nothing drives it yet.

**Check:** the Pi's green LED flickers (it's reading the card).

---

## Phase 3 — Connect to the Pi and prep the system

From your computer's terminal (Terminal on macOS/Linux, or PowerShell/Windows Terminal):

```bash
# connect over the network (password is the one you set in imager)
ssh fiwi42@weatherpi.local
```

If `weatherpi.local` can't be found, find the Pi's IP from your router and use `ssh fiwi42@<ip-address>` instead.

Once you're in, update the system and enable the two interfaces the display needs (**SPI** for pixel data, **I2C** for the display's auto‑detect chip):

```bash
# update package lists and installed packages
sudo apt update && sudo apt full-upgrade -y

# turn on SPI and I2C non-interactively
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0

# reboot so the interfaces come up
sudo reboot
```

The reboot drops your SSH session. Wait ~30s and reconnect with the same `ssh` command.

**Check:** after reconnecting, `ls /dev/spidev*` lists at least one `spidev` device.

---

## Phase 4 — Put the project on the Pi

Clone from GitHub (recommended — enables auto-updates):

```bash
# On the Pi: generate an SSH key and add it to GitHub
ssh-keygen -t ed25519 -C "pi-weather-station" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
# → copy this output to GitHub → Settings → SSH keys → New SSH key

# Then clone
git clone git@github.com:finnwimberly/weather-station.git ~/weather_station
```

Back in the **SSH session** on the Pi, install the system libraries that are slow to build from source, then make a virtual environment that can see them:

```bash
# heavy libs from apt (much faster than pip-building on a Pi 3)
sudo apt install -y python3-venv python3-pip python3-numpy python3-pil python3-matplotlib

cd ~/weather_station

# venv that can also see the apt-installed packages above
python3 -m venv --system-site-packages venv
source venv/bin/activate

# the inky driver (with Pi GPIO/SPI extras) plus the remaining pure-python deps
pip install "inky[rpi,fonts]>=2.0.0" requests
```

**Check:** `python -c "import inky, requests, PIL, matplotlib; print('imports ok')"` prints `imports ok`.

---

## Phase 5 — Configure for your location

Your email is already set in `config.py` (NOAA's free APIs want a contact address). The defaults cover the Mass/NH/RI coast and four buoys, so you may not need to change anything. If you want the **forecast** panel centered somewhere specific, edit these lines in `config.py`:

```python
FORECAST_LAT = 42.36   # your latitude
FORECAST_LON = -71.05  # your longitude
```

`UNITS = "imperial"` controls °F / mph / ft vs metric — flip it to `"metric"` if you prefer.

---

## Phase 6 — Test (no hardware first, then the real screen)

Still in the venv on the Pi:

```bash
# 1) data-only sanity check: fetches NOAA data and prints a dashboard
python main.py

# 2) render all six screens to PNGs (no display needed) — proves the graphics work
python main.py --render --all
ls output/        # you'll see map / wave / forecast PNGs
```

If those look right, drive the **actual display**:

```bash
# render the current screen to the e-paper
python main.py --render --epaper
```

> The 7.3" e‑paper is slow on purpose — a full refresh takes **30–40 seconds** and you'll see it flash through colors. That's normal, not a fault.

**Check:** an image appears on the Inky.

---

## Phase 7 — Test the buttons

The four buttons are on the long edge of the Inky. Run the listener by hand first:

```bash
python -m display.buttons
```

Press each button — the terminal logs which mode it set:

- **A** → Map
- **B** → Wave
- **C** → Forecast
- **D** → next wave screen (cycles the four buoy/beach pairings)

The listener only writes the chosen mode to a small state file; the main loop (next phase) is what re-reads it and repaints the screen. Press **Ctrl+C** to stop the listener.

**Check:** each press prints a line like `Button A -> map mode`.

---

## Phase 8 — Start automatically on boot

This makes the station run the display loop and the button listener every time it powers up, with no SSH needed.

```bash
# the service files are already set to user "fiwi42" — no edits needed.
# (only if you reflash with a different username would you edit them:
#   nano ~/weather_station/deploy/weather-station.service
#   nano ~/weather_station/deploy/weather-buttons.service )

# install both services
sudo cp ~/weather_station/deploy/weather-station.service /etc/systemd/system/
sudo cp ~/weather_station/deploy/weather-buttons.service /etc/systemd/system/

# load, enable on boot, and start now
sudo systemctl daemon-reload
sudo systemctl enable --now weather-station.service weather-buttons.service
```

Check they're alive:

```bash
systemctl status weather-station.service
systemctl status weather-buttons.service
```

Both should read **active (running)**. Reboot once (`sudo reboot`) to confirm the display comes back on its own.

**Check:** after a cold boot, the screen populates within a minute or two and the buttons change modes.

---

## Phase 9 — Enable auto-updates from GitHub

Install the timer that polls GitHub every 5 minutes and restarts the display service if new code is found:

```bash
chmod +x ~/weather_station/deploy/auto-update.sh

# allow passwordless restart of the weather-station service
echo "fiwi42 ALL=(ALL) NOPASSWD: /bin/systemctl restart weather-station" | sudo tee /etc/sudoers.d/weather-station-update

sudo cp ~/weather_station/deploy/auto-update.service /etc/systemd/system/
sudo cp ~/weather_station/deploy/auto-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now auto-update.timer
```

After this, pushing to GitHub on your Mac deploys to the Pi automatically within 5 minutes. Check `~/weather_station/update.log` to see what was pulled.

---

## Troubleshooting

**Display stays blank / "auto detection failed."** SPI or I2C isn't on. Re-run the Phase 3 `raspi-config` commands and reboot. Confirm with `ls /dev/spidev*`.

**`scp`/`ssh` can't find `weatherpi.local`.** Your network doesn't resolve `.local` names — use the Pi's IP address (from your router) instead.

**Buttons do nothing.** Check the listener service: `systemctl status weather-buttons.service`. If it errored on `gpiod`, run `pip install gpiod gpiodevice` inside the venv. Also confirm `gpioinfo` runs without error.

**Colors look washed out.** Adjust `saturation` in `display/render.py`'s `display_to_epaper` (currently `1.0`; try `0.7`–`0.9`).

**Everything is slow.** It's a Pi 3 and ACeP e‑paper — refreshes of 30–40s are normal. Data refreshes on a 30‑minute timer, so this isn't a live feed.

**See the logs.** `journalctl -u weather-station.service -f` follows the main loop's output live.

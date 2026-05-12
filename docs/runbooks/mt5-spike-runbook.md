# MT5 bridge spike — runbook

Goal: prove (or disprove) that a Python process on a Windows VPS can place,
observe, and close a market order on an FTMO MT5 demo, with SL/TP attached,
in under 2 s round-trip. Day-1 task. Throwaway host.

**This is the spike host, not the production VPS.** Production needs one VPS
per prop firm (separate IPs) — that comes in Phase 1, not now.

---

## 1. Provision Windows VPS (~15–30 min)

Pick one. Europe region for FTMO latency (FTMO infra is Prague-adjacent).

| Provider | Plan | RAM | Region | Cost (May 2026) |
|---|---|---|---|---|
| **Contabo** | VPS S Windows | 8 GB | Frankfurt (EU) | ~$11/mo |
| **FXVM** | Basic FX VPS | 4 GB | London / Frankfurt / NY | ~$25/mo |
| **AccuWeb** | Cheap Windows VPS L1 | 4 GB | Amsterdam | ~$13/mo |

Pick **Contabo VPS S Windows in Frankfurt** for the spike — cheapest, EU,
fine specs. FXVM is a fallback if Contabo has provisioning lag.

Required: Windows Server 2022, ≥4 GB RAM, ≥40 GB SSD, static public IP, RDP.

Save the VPS hostname/IP, Administrator password, and RDP file locally
(1Password or equivalent). Do **not** put them in the repo.

## 2. RDP into the VPS (5 min)

- macOS: install "Windows App" from the App Store (the rebranded Microsoft
  Remote Desktop). Connect to `vps.host:3389` with Administrator creds.
- Confirm clock is UTC and that Windows Update is current.

## 3. FTMO MT5 demo signup (10 min)

1. Go to **https://ftmo.com/en/free-trial/** (free trial; no payment).
2. Fill: name, email, country (Costa Rica is fine), platform = **MT5**,
   account size **$10,000** (cheapest, sufficient for spike).
3. Submit. Confirmation email arrives within ~2 min with:
   - **Login** (integer account number)
   - **Password** (investor + master — use master)
   - **Server** name, e.g. `FTMO-Demo` or `FTMO-Demo2`

Record all three exactly — typos in the server string are the #1 cause of
`initialize` returning False.

## 4. Install MT5 terminal on VPS (10 min)

1. On the VPS, browser to **https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe**.
   *(FTMO's branded link redirects here. Use the upstream URL — branded
   installers occasionally lag behind on Windows Server 2022 compat.)*
2. Install with defaults. Path: `C:\Program Files\MetaTrader 5`.
3. On first launch, **File → Login to Trade Account** → paste login,
   password, pick the FTMO server from the dropdown (do not type it — pick).
4. Verify: bottom-right shows green connection bar, kbps flowing.
5. Open an EURUSD chart. Confirm ticks stream.

## 5. Install Python 3.11+ on VPS (5 min)

1. Browser to **https://www.python.org/downloads/windows/**. Grab the
   latest **Windows installer (64-bit)** for Python 3.11, 3.12, 3.13, or
   3.14 — `MetaTrader5` 5.0.5735 ships wheels for all four (cp311..cp314).
   3.12 is what the dev `.venv` uses; matching it is convenient but not
   required for the spike host.
2. Install. Check **"Add python.exe to PATH"** on the first screen.
3. Open **PowerShell** (not WSL — `MetaTrader5` pkg is Windows-native and
   will not see the terminal from inside WSL).
4. Verify:
   ```powershell
   python --version          # Python 3.11+ acceptable
   python -m pip install --upgrade pip
   python -m pip install MetaTrader5
   python -c "import MetaTrader5; print(MetaTrader5.__version__)"
   ```

## 6. Drop credentials on VPS only (3 min)

Create `C:\Users\Administrator\.propfarm-secrets.json` with exactly:

```json
{"ftmo_demo": {"login": 1234567, "password": "REPLACE_ME", "server": "FTMO-Demo"}}
```

- `login` is an **int**, no quotes.
- `password` and `server` are strings, exact case.
- Restrict ACL: right-click → Properties → Security → remove inherited
  permissions, leave only Administrator with Full Control. Or PowerShell:
  ```powershell
  icacls "$HOME\.propfarm-secrets.json" /inheritance:r /grant:r "Administrator:F"
  ```

**Never** copy this file to the macOS laptop. **Never** commit it. Keep it
outside the repo tree entirely — in `C:\Users\Administrator\` as instructed,
not under the cloned project directory. The repo's `.gitignore` also matches
`.propfarm-secrets.json` and `**/.propfarm-secrets.json` defensively, so even
a misplaced copy inside the tree would be ignored — but the primary control
is "the file does not live inside the repo, ever."

## 7. Run the spike (1 min)

On the VPS, in PowerShell:

```powershell
cd $HOME
# Pull just this script (don't clone the whole repo on the spike host).
# Easiest: paste-create scripts\spike_mt5.py from the macOS repo via clipboard.
python .\scripts\spike_mt5.py
```

Expected stdout (one line):

```
send rtt_ms=180.4 retcode=10009
```

`10009` is `TRADE_RETCODE_DONE`. Script exits 0 silently after the close.

## 8. Pass / fail criteria

PASS — all of:
- Script exits 0.
- The printed `send rtt_ms` is < 2000.
- In the MT5 terminal **Trade** tab, the position appeared (briefly) with
  SL and TP both populated.
- In the **History** tab, the position is closed with two deals (in + out)
  and Comment column shows no rejection text.
- `mt5.last_error()` is never logged (assertion never fires).

FAIL — any of:
- `AssertionError` on `mt5.initialize` → wrong creds, wrong server, or
  MT5 terminal not running / not logged in.
- `result.retcode != 10009` → broker rejection. Common causes:
  `10018` market closed, `10019` no money (demo balance not funded —
  re-check FTMO email), `10027` autotrading disabled (enable via the
  terminal's "Algo Trading" button), `10030` unsupported filling mode
  (try `ORDER_FILLING_FOK`).
- RTT > 2000 ms → VPS region is wrong; reprovision closer to FTMO.

## 9. What to paste back

Copy this and paste into the chat / STATUS.md:

```
spike_mt5.py result: PASS|FAIL
stdout: send rtt_ms=<X> retcode=<Y>
mt5 pkg version: <Z>
vps region: <Frankfurt|...>
notes: <anything weird>
```

Redact: do not paste the contents of `.propfarm-secrets.json`. The script
output has no creds in it — safe to paste verbatim.

# Cisco Switch Factory Reset Automation

Python automation scripts for performing factory resets on enterprise Cisco switches via SecureCRT console sessions. Eliminates manual command entry — fully automated from boot recovery through post-reload setup.

---

## Scripts

### `cisco_n9k_reset.py` — Cisco Nexus 9000 (NX-OS)
Automates the full factory reset and wipe process for the Cisco Nexus 9000 series running NX-OS. Handles boot recovery mode, bootflash cleanup, startup-config erase, and post-reload setup. Includes retry logic built around Cisco Field Notice FN-70390, a known hardware issue affecting the N9K reload sequence.

### `cisco_4500x_reset.py` — Cisco Catalyst 4500-X (IOS)
Automates the factory reset process for the Cisco Catalyst 4500-X running classic IOS. Handles boot recovery mode, startup-config erase, and post-reload setup automation via console session.

---

## Tools & Environment
- **Language:** Python
- **Terminal:** SecureCRT (console session)
- **Platforms:** Cisco NX-OS · Cisco IOS
- **Editor:** VS Code + Claude Code
- **Tested on:** Cisco N9K-C9348GC-FXP (NX-OS) · Cisco WS-C4500X-16 (IOS)

---

## What Each Script Does
1. Connects via SecureCRT console session
2. Enters boot recovery mode
3. Cleans bootflash / erases startup-config
4. Handles reload sequence with error recovery
5. Automates post-reload initial setup

---

## Author
**Andrew Boakye** — IT Support Professional & AI Builder  
[Portfolio](https://boakyeandrew.tech) · [LinkedIn](https://linkedin.com/in/andrewboakye)

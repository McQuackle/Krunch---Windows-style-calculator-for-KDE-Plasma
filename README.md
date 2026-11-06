# Krunch — Everyday Calculator

A friendly everyday calculator for KDE Plasma with a familiar layout for anyone coming from Windows, plus history, themes and easy copy/paste.

## Install
```bash
unzip krunch.zip && cd krunch
bash install.sh
```
Only dependency: `python3-pyqt6` from Fedora's official repos. Opens from the app launcher as **Krunch**.
Uninstall: `bash uninstall.sh` (add `--purge` to delete settings and history too).

## Layout
Opens dark (Graphite theme) in **Standard** mode — just the everyday buttons. Use the ☰ menu (or Alt+1 / Alt+2) to switch to **Scientific**, which adds sin, cos, tan, log and friends. Pick other themes in ☰ → Settings & colors.

## Using it
- Type, click, or paste. `$1,250.00 × 3` just works — currency signs and commas are ignored.
- Paste a column of numbers from a spreadsheet and they're added together.
- `200 + 15%` = 230 · `200 × 15%` = 30 · `17 mod 5` = 2 · `2π`, `3(4+1)`, `√9` all work.
- The live result shows above the big number as you type. `ans` = your last result.
- After `=`, typing a number starts fresh; typing an operator continues from the result.
- Uses exact decimal math (like the Windows calculator): 0.1 + 0.2 = 0.3, and 1 ÷ 3 = × 3 = gives exactly 1.

## History
Toggle with the clock icon or Ctrl+H. Search box filters. Double-click an entry to insert its result.
Right-click: insert result, edit the calculation, copy result / calculation / full line, delete.
Select several (Ctrl/Shift+click) and press Ctrl+C to copy them as lines. Delete key removes.

## Themes
**☰ → Match KDE colors** is a one-click toggle: every color, text included, comes from your Plasma color scheme and updates live when you change it. Turn it off to go back to your own theme (also in Settings → Colors & Look).
**☰ → Settings & colors → Colors & Look.** Presets: Graphite (default dark), Match KDE, Midnight, Paper, Nord, Forest, Sunset, High Contrast.
Every color is editable, and each one has a **Follow KDE** checkbox so you can mix — e.g. KDE text and
backgrounds with your own button colors (one-click button for that). KDE-linked colors update live when
you change your color scheme in System Settings. Text size, roundness and spacing are adjustable.

## Shortcuts
Enter or `=` calculate · Esc clear all · Delete clear entry · Ctrl+C copy result (when nothing is selected) · Ctrl+H history · Ctrl+, settings · Ctrl+Z undo

Files: settings in `~/.config/krunch/settings.json`, history in `~/.local/share/krunch/history.json`.

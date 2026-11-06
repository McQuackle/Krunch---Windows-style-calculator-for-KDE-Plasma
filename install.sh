#!/usr/bin/env bash
# Installs Krunch for the current user. Dependencies come from Fedora's official repos only.
set -euo pipefail
cd "$(dirname "$0")"

echo "→ Installing PyQt6 from the Fedora repos (asks for your password)…"
sudo dnf install -y python3-pyqt6

echo "→ Installing Krunch to ~/.local…"
install -Dm755 krunch.py "$HOME/.local/bin/krunch"
install -Dm644 krunch.svg "$HOME/.local/share/icons/hicolor/scalable/apps/krunch.svg"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/krunch.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Krunch
GenericName=Calculator
Comment=Everyday calculator with history, themes and copy/paste
Exec=$HOME/.local/bin/krunch
Icon=krunch
Terminal=false
Categories=Utility;Calculator;Qt;
Keywords=calculator;math;calc;krunch;
StartupWMClass=krunch
EOF

# Remove the old "Tally" launcher if it's there (your settings and history move over automatically)
rm -f "$HOME/.local/bin/tally" "$HOME/.local/share/applications/tally.desktop"

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
kbuildsycoca6 2>/dev/null || true

echo "✓ Done. Find “Krunch” in your app launcher, or run: ~/.local/bin/krunch"

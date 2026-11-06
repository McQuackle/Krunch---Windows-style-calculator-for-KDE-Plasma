#!/usr/bin/env bash
# Removes Krunch. Add --purge to also delete your settings and history.
set -eu
rm -f "$HOME/.local/bin/krunch" "$HOME/.local/share/applications/krunch.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/krunch.svg"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/.config/krunch" "$HOME/.local/share/krunch"
fi
kbuildsycoca6 2>/dev/null || true
echo "✓ Krunch removed. (PyQt6 was left installed; remove with: sudo dnf remove python3-pyqt6)"

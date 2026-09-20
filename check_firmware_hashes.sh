#!/bin/bash
# ============================================================
# check_firmware_hashes.sh
# Verifies all md5/md5sum hashes in the firmware directory
# ============================================================

# Anchored to the repo root so the script works from any directory.
cd "$(dirname "$(readlink -f "$0")")" || exit 1

FIRMWARE_DIR="firmware"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

if [ ! -d "$FIRMWARE_DIR" ]; then
    echo -e "${RED}Error: Directory '$FIRMWARE_DIR' does not exist.${NC}"
    exit 1
fi

echo -e "Verifying all firmware hashes in '${FIRMWARE_DIR}/'..."
echo "============================================================"

# Navigate to firmware dir so md5sum paths resolve correctly
cd "$FIRMWARE_DIR" || exit 1

has_hashes=0
success_count=0
fail_count=0

# Loop through both .md5 and .md5sum extensions just in case
for hash_file in *.md5sum *.md5; do
    if [ -f "$hash_file" ]; then
        has_hashes=1
        echo -e "\nChecking $hash_file:"
        
        if md5sum -c "$hash_file"; then
            ((success_count++))
        else
            ((fail_count++))
        fi
    fi
done

echo -e "\n============================================================"
if [ "$has_hashes" -eq 0 ]; then
    echo -e "${RED}No hash files (*.md5 or *.md5sum) found in $FIRMWARE_DIR/.${NC}"
    echo -e "  Create one per image:  md5sum EOS64-4.35.5M.swi > EOS64-4.35.5M.swi.md5"
    # No checksum at all is a failure too: the bootstrap then installs an
    # unverified image and only logs a warning.
    exit 1
fi

echo -e "Summary:"
if [ "$fail_count" -eq 0 ]; then
    echo -e "  ${GREEN}All $success_count hash checks passed successfully!${NC}"
    exit 0
fi

echo -e "  ${RED}Warning: $fail_count hash check(s) FAILED!${NC} ($success_count passed)"
# Exit non-zero so a caller or CI job actually notices a corrupt image.
exit 1

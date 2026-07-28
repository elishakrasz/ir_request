#!/usr/bin/env bash
# Export the EngagementDashboard solution from DEV and unpack declarative source
# into solution/src/ (the re-creatable artifact checked into git).
#
# Run AFTER provisioning + the maker-portal rollup step (docs/schema.md), so the
# export captures the complete schema including rollups.
#
# Prereq: pac auth create --url https://exigentcrmdev.crm4.dynamics.com/
#         (interactive, operator account; known-good pac version 1.43.6 — see CLAUDE.md)
#
# PROD note (house rule 1): the managed zip produced here is imported to PROD
# BY HAND via the maker portal only — never by CLI.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p build

pac solution export --name EngagementDashboard --path build/EngagementDashboard.zip --managed false --overwrite
pac solution export --name EngagementDashboard --path build/EngagementDashboard_managed.zip --managed true --overwrite

# Declarative source of truth, committed to git
pac solution unpack --zipfile build/EngagementDashboard.zip --folder src --packagetype Unmanaged --allowDelete

echo
echo "Unpacked to solution/src/ — review 'git diff solution/src' and commit."
echo "PROD import artifact: solution/build/EngagementDashboard_managed.zip (manual maker-portal import only)."

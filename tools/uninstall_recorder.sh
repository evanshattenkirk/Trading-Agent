#!/bin/bash
# Stops and removes the recorder LaunchAgent. Keeps recorded data (~/.agentdesk/journal.db) and the recorder's
# Robinhood sign-in (~/.agentdesk/recorder); delete ~/.agentdesk/recorder-app yourself if you want the copy gone.
LABEL="com.agentdesk.recorder"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed $LABEL."

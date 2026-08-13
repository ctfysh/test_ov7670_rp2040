#!/bin/bash
# Switch Photo Booth's camera to the YD RP2040.
#
# Photo Booth does NOT auto-select our board: it defaults to the built-in
# FaceTime camera. A freshly opened PB showing FaceTime is a FALSE ALARM for
# the wedge - 'Q' will read str=0 until you manually switch in the
# "摄像头" menu. This script does exactly the manual click.
#
# Menu path: menu item "YD RP2040" of menu "摄像头" of menu bar 1
# (the PB menubar is Apple / Photo Booth / 文件 / 编辑 / 显示 / 摄像头 / 窗口 / 帮助)
#
# Note: on the FIRST switch attempt PB sometimes bounces back to FaceTime
# (macOS is still probing the freshly-enumerated PID). Run it twice if the
# second query still shows str=0.
#
# Usage: pb_switch_cam.sh
osascript <<'EOF'
tell application "Photo Booth" to activate
delay 0.5
tell application "System Events"
    tell process "Photo Booth"
        click menu item "YD RP2040" of menu "摄像头" of menu bar 1
    end tell
end tell
EOF

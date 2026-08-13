// Print the on-screen bounds of the Photo Booth window, as "X Y W H".
// Used by verify_wedge_fix.py to crop screencapture output to the PB window.
// Usage: swift winid.swift
import CoreGraphics

let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as! [[String: Any]]
for w in list {
    let owner = w[kCGWindowOwnerName as String] as? String ?? ""
    if owner.contains("Photo") {
        if let b = w[kCGWindowBounds as String] as? [String: Any] {
            let x = b["X"] as? Int ?? 0
            let y = b["Y"] as? Int ?? 0
            let wd = b["Width"] as? Int ?? 0
            let ht = b["Height"] as? Int ?? 0
            print("\(x) \(y) \(wd) \(ht)")
        }
    }
}

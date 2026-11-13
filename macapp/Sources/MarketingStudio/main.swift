import Cocoa

// Single-instance guard: a second copy would start a second engine and fight
// over the port, so ask the running one to show its window and bail.
let myPid = ProcessInfo.processInfo.processIdentifier
if let bundleId = Bundle.main.bundleIdentifier,
   let other = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId)
       .first(where: { $0.processIdentifier != myPid }) {
    DistributedNotificationCenter.default().postNotificationName(
        AppDelegate.reopenNotification, object: nil, userInfo: nil,
        deliverImmediately: true)
    other.activate(options: [])
    NSLog("Another Marketing Studio is already running - asked it to show its window")
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

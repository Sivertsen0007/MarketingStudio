import Cocoa
import WebKit
import Darwin

/// Marketing Studio's native shell (pattern from Mattdaemon).
///
/// On launch it starts the bundled Python engine
/// (Contents/Resources/engine/server.py --port <port> --no-open) as a child
/// process, waits until http://127.0.0.1:<port>/ answers, then shows it in one
/// big WKWebView window. If the port already answers with Marketing Studio
/// (e.g. a dev server), it just attaches. The engine it started is stopped on
/// quit; an engine it merely attached to is left alone.
///
/// Overrides (dev/testing):
///   MSTUDIO_PORT=<n>         port to use (default 4830); also `--port <n>`
///   MSTUDIO_ENGINE_DIR=<dir> run server.py from this folder instead of the bundle
final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate,
                         WKDownloadDelegate, NSWindowDelegate {
    static let reopenNotification =
        Notification.Name("com.sivertsen.marketingstudio.reopen")
    static let frameKey = "MarketingStudioMainWindow"

    private var window: NSWindow?
    private var webView: WKWebView!
    private var server: Process?
    private var logHandle: FileHandle?
    private var port = 4830
    private var terminating = false
    private var sigtermSource: DispatchSourceSignal?

    /// MSTUDIO_HEADLESS_TEST=1: never show a modal dialog (automated launch tests);
    /// fatal problems are logged and the app exits instead.
    private let headless = ProcessInfo.processInfo.environment["MSTUDIO_HEADLESS_TEST"] == "1"

    private var logURL: URL {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/MarketingStudio", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("engine.log")
    }

    // MARK: - Life cycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        port = AppDelegate.resolvePort()
        // `kill <pid>` (and the updater's swap script fallback) quits like Cmd-Q,
        // so the engine we started is stopped instead of being orphaned.
        signal(SIGTERM, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        src.setEventHandler { NSApp.terminate(nil) }
        src.resume()
        sigtermSource = src
        buildMenu()
        buildWindow()

        DistributedNotificationCenter.default().addObserver(
            self, selector: #selector(handleReopenRequest),
            name: AppDelegate.reopenNotification, object: nil)

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let probe = self.probe(port: self.port)
            DispatchQueue.main.async { self.boot(after: probe) }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        terminating = true
        if let w = window { w.saveFrame(usingName: AppDelegate.frameKey) }
        stopServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { showWindow() }
        return true
    }

    @objc private func handleReopenRequest() {
        DispatchQueue.main.async { [weak self] in self?.showWindow() }
    }

    static func resolvePort() -> Int {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--port"), i + 1 < args.count, let p = Int(args[i + 1]), p > 0 {
            return p
        }
        if let raw = ProcessInfo.processInfo.environment["MSTUDIO_PORT"], let p = Int(raw), p > 0 {
            return p
        }
        return 4830
    }

    // MARK: - Engine

    private enum Probe { case studio, other, none }

    /// What answers on the port right now: Marketing Studio, something else, or nothing.
    private func probe(port: Int) -> Probe {
        guard let url = URL(string: "http://127.0.0.1:\(port)/") else { return .none }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 1.5)
        req.httpMethod = "GET"
        let sem = DispatchSemaphore(value: 0)
        var result = Probe.none
        let task = URLSession.shared.dataTask(with: req) { data, response, error in
            defer { sem.signal() }
            if error != nil { result = .none; return }
            let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
            if body.contains("name=\"studio-press\"") && body.contains("Marketing Studio") {
                result = .studio
            } else if response != nil {
                result = .other
            }
        }
        task.resume()
        _ = sem.wait(timeout: .now() + 3)
        return result
    }

    private func boot(after probe: Probe) {
        switch probe {
        case .studio:
            NSLog("Marketing Studio already answers on \(port) - attaching")
            loadStudio()
        case .other:
            fatalStart("Port \(port) is already used by another program. Quit it, or start Marketing Studio with MSTUDIO_PORT set to a free port.")
        case .none:
            guard let script = engineScript() else {
                fatalStart("The bundled engine (Contents/Resources/engine/server.py) is missing - reinstall Marketing Studio.")
                return
            }
            startServer(script: script)
            waitForStudio(deadline: Date().addingTimeInterval(30))
        }
    }

    private func engineScript() -> String? {
        if let dir = ProcessInfo.processInfo.environment["MSTUDIO_ENGINE_DIR"], !dir.isEmpty {
            let p = (dir as NSString).expandingTildeInPath + "/server.py"
            return FileManager.default.fileExists(atPath: p) ? p : nil
        }
        guard let res = Bundle.main.resourcePath else { return nil }
        let p = res + "/engine/server.py"
        return FileManager.default.fileExists(atPath: p) ? p : nil
    }

    private func startServer(script: String) {
        let process = Process()
        var env = ProcessInfo.processInfo.environment
        // A Finder-launched app inherits launchd's minimal PATH; add the usual
        // Homebrew / user prefixes so python3 (and anything it runs) is found.
        let home = env["HOME"] ?? NSHomeDirectory()
        let extra = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
        env["PATH"] = (extra + [env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"]).joined(separator: ":")
        env["MSTUDIO_APP"] = "1"
        env["MSTUDIO_VERSION"] = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
        // Never write __pycache__ into the signed bundle (it would break the seal).
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env

        let args = [script, "--port", String(port), "--no-open"]
        if FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") {
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = args
        } else {
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["python3"] + args
        }
        process.currentDirectoryURL = URL(fileURLWithPath: script).deletingLastPathComponent()

        // Engine output goes to ~/Library/Logs/MarketingStudio/engine.log.
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let h = try? FileHandle(forWritingTo: logURL) {
            h.truncateFile(atOffset: 0)
            logHandle = h
            process.standardOutput = h
            process.standardError = h
        }
        process.terminationHandler = { [weak self] proc in
            NSLog("Marketing Studio engine exited with status \(proc.terminationStatus)")
            DispatchQueue.main.async { self?.engineExited(status: proc.terminationStatus) }
        }
        do {
            try process.run()
            server = process
            NSLog("Started Marketing Studio engine (pid \(process.processIdentifier)) on port \(port)")
        } catch {
            fatalStart("Could not start the engine: \(error.localizedDescription)")
        }
    }

    private var studioLoaded = false

    private func engineExited(status: Int32) {
        if terminating { return }
        let tail = logTail()
        if headless {
            NSLog("Marketing Studio engine stopped (exit \(status)): \(tail)")
            if !studioLoaded { fatalStart("engine stopped before it was ready (exit \(status))") }
            return
        }
        if !studioLoaded {
            fatalStart("The engine stopped before it was ready (exit \(status)).\n\n\(tail)\n\nFull log: \(logURL.path)")
        } else {
            let alert = NSAlert()
            alert.messageText = "The Marketing Studio engine stopped"
            alert.informativeText = "Exit \(status).\n\n\(tail)\n\nFull log: \(logURL.path)\nQuit and open Marketing Studio again to restart it."
            alert.alertStyle = .warning
            alert.addButton(withTitle: "OK")
            alert.runModal()
        }
    }

    private func logTail() -> String {
        guard let text = try? String(contentsOf: logURL, encoding: .utf8) else { return "" }
        return text.split(separator: "\n", omittingEmptySubsequences: false).suffix(12).joined(separator: "\n")
    }

    private func waitForStudio(deadline: Date) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            while Date() < deadline {
                if let s = self.server, !s.isRunning { return }   // engineExited reports it
                if self.probe(port: self.port) == .studio {
                    DispatchQueue.main.async { self.loadStudio() }
                    return
                }
                usleep(200_000)
            }
            DispatchQueue.main.async {
                self.fatalStart("The engine did not answer on port \(self.port) within 30 seconds.\n\n\(self.logTail())")
            }
        }
    }

    private func stopServer() {
        guard let server = server, server.isRunning else { return }
        server.terminationHandler = nil
        server.terminate()
        let deadline = Date().addingTimeInterval(3)
        while server.isRunning && Date() < deadline { usleep(50_000) }
        if server.isRunning { kill(server.processIdentifier, SIGKILL) }
        self.server = nil
    }

    // MARK: - Window / web view

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.preferences.javaScriptCanOpenWindowsAutomatically = true
        let wv = WKWebView(frame: .zero, configuration: config)
        wv.autoresizingMask = [.width, .height]
        wv.uiDelegate = self
        wv.navigationDelegate = self
        if #available(macOS 13.3, *) { wv.isInspectable = true }
        wv.pageZoom = savedZoom()  // U26: the zoom from the last session
        webView = wv

        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let win = NSWindow(contentRect: screen,
                           styleMask: [.titled, .closable, .miniaturizable, .resizable],
                           backing: .buffered, defer: false)
        win.title = "Marketing Studio"
        win.isReleasedWhenClosed = false
        win.delegate = self
        win.contentView = wv
        win.collectionBehavior.insert(.fullScreenPrimary)
        win.minSize = NSSize(width: 900, height: 600)
        // First launch: fill the screen it lands on (maximised). After that:
        // where you left it. Fitted AFTER ordering front, because macOS may put
        // a new window on another display (the active Space) when it is shown.
        let restored = win.setFrameUsingName(AppDelegate.frameKey)
        win.makeKeyAndOrderFront(nil)
        if !restored, let s = win.screen ?? NSScreen.main {
            win.setFrame(s.visibleFrame, display: true)
        }
        win.setFrameAutosaveName(AppDelegate.frameKey)
        win.makeFirstResponder(wv)
        window = win
        showLoading()
        NSApp.activate(ignoringOtherApps: true)
    }

    private func showLoading() {
        let html = """
        <html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;\
        font:15px -apple-system,sans-serif;color:#5f6368;background:#fff">Starting Marketing Studio...</body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    /// A marketingstudio://join?server=..&invite=.. link (team invite) waiting to be handed to the page.
    private var pendingJoin: String?
    /// A marketingstudio://open?coll=..&id=.. link (from a team notification email or webhook): its query, for the page.
    private var pendingOpen: String?

    /// Team invite links: the page gets them as #team-join=<link> and opens Setup > Team with the link filled in
    /// (the person still presses Join). Works on a cold start too: the link waits until the engine answers.
    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls where url.scheme?.lowercased() == "marketingstudio" {
            if url.host?.lowercased() == "join" { pendingJoin = url.absoluteString }
            if url.host?.lowercased() == "open" { pendingOpen = url.query ?? "" }
        }
        guard pendingJoin != nil || pendingOpen != nil, window != nil else { return }
        showWindow()
        if studioLoaded { loadStudio() }
    }

    private func loadStudio() {
        studioLoaded = true
        var address = "http://127.0.0.1:\(port)/"
        if let link = pendingJoin,
           let enc = link.addingPercentEncoding(withAllowedCharacters: .alphanumerics) {
            address += "#team-join=" + enc
            pendingJoin = nil
        } else if let query = pendingOpen,
                  let enc = query.addingPercentEncoding(withAllowedCharacters: .alphanumerics) {
            address += "#team-open=" + enc  // team_notify.js jumps to the item
            pendingOpen = nil
        }
        guard let url = URL(string: address) else { return }
        webView.load(URLRequest(url: url))
        if let w = window { w.makeFirstResponder(webView) }
    }

    private func showWindow() {
        if let w = window {
            if w.isMiniaturized { w.deminiaturize(nil) }
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        buildWindow()
        if studioLoaded { loadStudio() }
    }

    func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow, closing === window else { return }
        closing.saveFrame(usingName: AppDelegate.frameKey)
        if !terminating { NSApp.terminate(nil) }
    }

    // MARK: - Links, new windows, downloads

    private func isLocal(_ url: URL) -> Bool {
        let host = url.host ?? ""
        return (host == "127.0.0.1" || host == "localhost") && url.port == port
    }

    /// target=_blank and window.open: hand the URL to the default browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url, let scheme = url.scheme,
           ["http", "https", "mailto"].contains(scheme) {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.shouldPerformDownload {
            // <a download>: a blob:/data: link (made in the page) is saved by
            // WebKit (see WKDownloadDelegate); a real URL goes to the browser.
            if let url = navigationAction.request.url, url.scheme == "http" || url.scheme == "https" {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
            } else {
                decisionHandler(.download)
            }
            return
        }
        if let url = navigationAction.request.url,
           navigationAction.targetFrame?.isMainFrame ?? true,
           navigationAction.navigationType == .linkActivated,
           url.scheme == "http" || url.scheme == "https" || url.scheme == "mailto",
           !isLocal(url) {
            // A plain link off Studio: the browser, never inside the app window.
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let isAttachment = (navigationResponse.response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Disposition")?.lowercased().hasPrefix("attachment") ?? false
        if isAttachment || !navigationResponse.canShowMIMEType {
            if let url = navigationResponse.response.url, url.scheme == "http" || url.scheme == "https" {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
            } else {
                decisionHandler(.download)
            }
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    /// In-page (blob:/data:) downloads are saved to ~/Downloads and shown in Finder.
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser
        let name = suggestedFilename.isEmpty ? "download" : suggestedFilename
        var dest = dir.appendingPathComponent(name)
        let base = dest.deletingPathExtension().lastPathComponent, ext = dest.pathExtension
        var n = 1
        while FileManager.default.fileExists(atPath: dest.path) {
            dest = dir.appendingPathComponent(ext.isEmpty ? "\(base) (\(n))" : "\(base) (\(n)).\(ext)")
            n += 1
        }
        pendingDownloads[ObjectIdentifier(download)] = dest
        completionHandler(dest)
    }

    private var pendingDownloads: [ObjectIdentifier: URL] = [:]

    func downloadDidFinish(_ download: WKDownload) {
        if let dest = pendingDownloads.removeValue(forKey: ObjectIdentifier(download)) {
            NSWorkspace.shared.activateFileViewerSelecting([dest])
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        pendingDownloads.removeValue(forKey: ObjectIdentifier(download))
        NSLog("Download failed: \(error.localizedDescription)")
    }

    // MARK: - JS alert / confirm / prompt (WKWebView drops them without these)

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "Marketing Studio"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        present(alert) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "Marketing Studio"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        present(alert) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = "Marketing Studio"
        alert.informativeText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        present(alert) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    /// As a sheet on the main window (completion called exactly once), else app-modal.
    private func present(_ alert: NSAlert, done: @escaping (NSApplication.ModalResponse) -> Void) {
        if let w = window, w.isVisible, w.attachedSheet == nil {
            alert.beginSheetModal(for: w) { resp in
                done(resp)
                w.makeFirstResponder(self.webView)
            }
        } else {
            done(alert.runModal())
        }
    }

    /// <input type="file">: WebKit needs a panel from us or the click does nothing.
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        let finish: (NSApplication.ModalResponse) -> Void = { completionHandler($0 == .OK ? panel.urls : nil) }
        if let w = window { panel.beginSheetModal(for: w, completionHandler: finish) } else { panel.begin(completionHandler: finish) }
    }

    // MARK: - Menu (maps Cmd-C/V/X/Z/A into the web view; Cmd-R reloads)

    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "About Marketing Studio",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Marketing Studio", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "Hide Others",
                                         action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Marketing Studio", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        // U26: File > Close Window (Cmd-W). Studio has one window, so closing it quits, like the red button.
        let fileItem = NSMenuItem(); main.addItem(fileItem)
        let file = NSMenu(title: "File"); fileItem.submenu = file
        file.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit"); editItem.submenu = edit
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        let pasteStyle = edit.addItem(withTitle: "Paste and Match Style",
                                      action: #selector(NSTextView.pasteAsPlainText(_:)), keyEquivalent: "V")
        pasteStyle.keyEquivalentModifierMask = [.command, .option, .shift]
        edit.addItem(withTitle: "Delete", action: #selector(NSText.delete(_:)), keyEquivalent: "")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        edit.addItem(.separator())
        let find = edit.addItem(withTitle: "Find...", action: #selector(NSResponder.performTextFinderAction(_:)), keyEquivalent: "f")
        find.tag = NSTextFinder.Action.showFindInterface.rawValue

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View"); viewItem.submenu = view
        view.addItem(withTitle: "Reload", action: #selector(reloadPage(_:)), keyEquivalent: "r")
        view.addItem(.separator())
        view.addItem(withTitle: "Actual Size", action: #selector(zoomReset(_:)), keyEquivalent: "0")
        // U26: Cmd-= zooms in (no Shift needed); a hidden Cmd-+ does the same for keyboards where + has its own key
        view.addItem(withTitle: "Zoom In", action: #selector(zoomIn(_:)), keyEquivalent: "=")
        let zoomPlus = view.addItem(withTitle: "Zoom In", action: #selector(zoomIn(_:)), keyEquivalent: "+")
        zoomPlus.isHidden = true
        zoomPlus.allowsKeyEquivalentWhenHidden = true
        view.addItem(withTitle: "Zoom Out", action: #selector(zoomOut(_:)), keyEquivalent: "-")
        view.addItem(.separator())
        let fs = view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fs.keyEquivalentModifierMask = [.command, .control]

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window"); winItem.submenu = win
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        win.addItem(withTitle: "Fill Screen", action: #selector(fillScreen(_:)), keyEquivalent: "")
        win.addItem(.separator())
        win.addItem(withTitle: "Bring All to Front", action: #selector(NSApplication.arrangeInFront(_:)), keyEquivalent: "")
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }

    @objc private func reloadPage(_ sender: Any?) {
        if studioLoaded { webView?.reloadFromOrigin() }
    }
    // U26: page zoom is remembered between launches (UserDefaults "pageZoom", 0.5-3, 1 = actual size)
    private static let zoomKey = "pageZoom"
    private func savedZoom() -> CGFloat {
        let z = UserDefaults.standard.double(forKey: AppDelegate.zoomKey)
        return z >= 0.5 && z <= 3 ? CGFloat(z) : 1
    }
    private func setZoom(_ z: CGFloat) {
        let v = (min(3, max(0.5, z)) * 10).rounded() / 10  // steps of 0.1, no drift from adding 0.1 many times
        webView.pageZoom = v
        UserDefaults.standard.set(Double(v), forKey: AppDelegate.zoomKey)
    }
    @objc private func zoomIn(_ sender: Any?) { setZoom(webView.pageZoom + 0.1) }
    @objc private func zoomOut(_ sender: Any?) { setZoom(webView.pageZoom - 0.1) }
    @objc private func zoomReset(_ sender: Any?) { setZoom(1) }
    @objc private func fillScreen(_ sender: Any?) {
        guard let w = window, let s = w.screen ?? NSScreen.main else { return }
        w.setFrame(s.visibleFrame, display: true, animate: true)
    }

    // MARK: - Errors

    private func fatalStart(_ message: String) {
        if headless {
            NSLog("Marketing Studio could not start: \(message)")
            terminating = true
            stopServer()
            exit(1)
        }
        let alert = NSAlert()
        alert.messageText = "Marketing Studio could not start"
        alert.informativeText = message
        alert.alertStyle = .critical
        alert.addButton(withTitle: "Quit")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
        terminating = true
        stopServer()
        NSApp.terminate(nil)
    }
}

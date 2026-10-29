import AppKit
import Darwin
import Foundation

// The launcher owns one backend process group. Backend readiness is an inherited
// pipe protocol, never a request to a guessed port or an existing localhost app.
final class ClippieDelegate: NSObject, NSApplicationDelegate {
    private var backend: Process?
    private var output = Data()
    private var editorURL: URL?
    private var bootstrapURL: URL?
    private var statusItem: NSStatusItem?
    private var startupTimer: Timer?
    private var shutdownTimer: Timer?
    private var stopping = false
    private var smokeReady = false
    private var smokeDirectory: URL?
    private var errorShown = false
    private var dataDirectory: URL?
    private var stopCompletion: (() -> Void)?
    private var terminating = false
    private var stoppingGroup: pid_t?
    private var ownedGroup: pid_t?
    private var unconfirmedGroup: pid_t?
    private var maintenance: String?
    private var maintenanceResult: [String: Any]?
    private var maintenanceError: String?
    private var expectedRestoreID: String?
    private var failedCode: String?
    private var statusMenus: [NSMenuItem] = []
    private let smoke = CommandLine.arguments.contains("--smoke-test")
    private let startupTimeout: TimeInterval = 120

    func applicationDidFinishLaunching(_ notification: Notification) {
        if smoke { NSApp.setActivationPolicy(.prohibited) }
        configureMenus()
        do { try startBackend() }
        catch { fail("Clippie could not start: \(error.localizedDescription)") }
    }

    private func configureMenus() {
        let menu = NSMenu()
        let root = NSMenuItem()
        let appMenu = NSMenu()
        let open = NSMenuItem(title: "Open Clippie", action: #selector(openEditor), keyEquivalent: "o")
        open.target = self
        appMenu.addItem(open)
        let status = NSMenuItem(title: "Starting Clippie…", action: nil, keyEquivalent: "")
        appMenu.addItem(status)
        statusMenus.append(status)
        let recovery = NSMenuItem(title: "Recovery…", action: #selector(openRecovery), keyEquivalent: "")
        recovery.target = self
        appMenu.addItem(recovery)
        appMenu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Clippie", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        appMenu.addItem(quit)
        root.submenu = appMenu
        menu.addItem(root)
        NSApp.mainMenu = menu
        if !smoke {
            let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            item.button?.title = "Clippie"
            item.menu = appMenu.copy() as? NSMenu
            if let copiedStatus = item.menu?.items.first(where: { $0.title == status.title }) {
                statusMenus.append(copiedStatus)
            }
            statusItem = item
        }
    }

    private func startBackend(arguments: [String] = [], operation: String? = nil) throws {
        if let group = unconfirmedGroup {
            guard kill(-group, 0) == -1, errno == ESRCH else {
                throw startupError("Clippie workers have not all stopped. Quit Clippie and try again before using Recovery.")
            }
            unconfirmedGroup = nil
        }
        output.removeAll()
        ownedGroup = nil
        editorURL = nil
        bootstrapURL = nil
        errorShown = false
        failedCode = nil
        maintenance = operation
        expectedRestoreID = operation == "restore" ? arguments.last : nil
        maintenanceResult = nil
        maintenanceError = nil
        setStatus(operation == nil ? "Starting Clippie…" : "Recovery in progress…")
        guard let resources = Bundle.main.resourceURL else {
            throw startupError("The application resources are missing. Reinstall Clippie.")
        }
        let python = resources.appendingPathComponent("python/bin/python3")
        let appRoot = resources.appendingPathComponent("app")
        let package = appRoot.appendingPathComponent("clippie")
        let binaries = resources.appendingPathComponent("bin")
        let manager = FileManager.default
        for required in [python, binaries.appendingPathComponent("ffmpeg"), binaries.appendingPathComponent("ffprobe")] {
            guard manager.isExecutableFile(atPath: required.path) else {
                throw startupError("The bundled \(required.lastPathComponent) is missing or cannot run. Reinstall Clippie.")
            }
        }
        guard manager.fileExists(atPath: package.appendingPathComponent("desktop.py").path) else {
            throw startupError("The backend entry point is missing. Reinstall Clippie.")
        }

        let data: URL
        if let existing = dataDirectory {
            data = existing
        } else if smoke {
            data = manager.temporaryDirectory.appendingPathComponent("clippie-launcher-\(UUID().uuidString)", isDirectory: true)
            smokeDirectory = data
        } else {
            data = try manager.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                   appropriateFor: nil, create: true).appendingPathComponent("Clippie", isDirectory: true)
        }
        dataDirectory = data
        try manager.createDirectory(at: data, withIntermediateDirectories: true,
                                    attributes: [.posixPermissions: 0o700])

        // Allowlist identity/locale only. Shell PYTHONPATH, API keys, model
        // overrides and a developer's virtualenv must not configure this app.
        let inherited = ProcessInfo.processInfo.environment
        var environment: [String: String] = [:]
        for name in ["HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"] {
            if let value = inherited[name] { environment[name] = value }
        }
        environment["PATH"] = binaries.path + ":/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PYTHONPATH"] = appRoot.path
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["CLIPPIE_RESOURCE_DIR"] = package.path
        environment["CLIPPIE_DATA_DIR"] = data.path
        environment["CLIPPIE_FFMPEG"] = binaries.appendingPathComponent("ffmpeg").path
        environment["CLIPPIE_FFPROBE"] = binaries.appendingPathComponent("ffprobe").path
        environment["CLIPPIE_LAUNCHER_PID"] = String(getpid())
        environment["CLIPPIE_DESKTOP"] = "1"
        environment["HF_HUB_OFFLINE"] = "1"
        environment["TRANSFORMERS_OFFLINE"] = "1"
        environment["HF_HOME"] = data.appendingPathComponent("cache/huggingface").path
        environment["XDG_CACHE_HOME"] = data.appendingPathComponent("cache").path
        environment["NUMBA_CACHE_DIR"] = data.appendingPathComponent("cache/numba").path
        if smoke { environment["CLIPPIE_SMOKE_TEST"] = "1" }

        let pipe = Pipe()
        let process = Process()
        process.executableURL = python
        process.arguments = ["-s", "-m", "clippie.desktop"] + arguments
        process.environment = environment
        process.currentDirectoryURL = data
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = pipe
        // Do not persist backend output or provider diagnostics automatically.
        // The desktop protocol must supply a safe error summary when available.
        process.standardError = FileHandle.nullDevice
        // Serialize nonblocking pipe drains with termination so final JSON is
        // consumed before the exit status, without waiting on inherited pipes.
        let reader = pipe.fileHandleForReading
        let descriptor = reader.fileDescriptor
        _ = fcntl(descriptor, F_SETFL, fcntl(descriptor, F_GETFL) | O_NONBLOCK)
        let readQueue = DispatchQueue(label: "Clippie.startup-protocol")
        let drain: () -> Data = {
            var result = Data()
            var buffer = [UInt8](repeating: 0, count: 8192)
            while result.count <= 64 * 1024 {
                let count = Darwin.read(descriptor, &buffer, buffer.count)
                if count <= 0 { break }
                result.append(contentsOf: buffer.prefix(count))
            }
            return result
        }
        reader.readabilityHandler = { [weak self] _ in
            readQueue.async {
                let bytes = drain()
                DispatchQueue.main.async {
                    guard let self, self.backend === process else { return }
                    self.receive(bytes)
                }
            }
        }
        process.terminationHandler = { [weak self] child in
            reader.readabilityHandler = nil
            readQueue.async {
                let bytes = drain()
                DispatchQueue.main.async {
                    guard let self, self.backend === child else { return }
                    self.receive(bytes)
                    guard self.backend === child else { return }
                    self.backendStopped(status: child.terminationStatus)
                }
            }
        }
        backend = process
        try process.run()
        resetStartupTimer()
    }

    private func setStatus(_ message: String) {
        for item in statusMenus { item.title = String(message.prefix(180)) }
    }

    private func resetStartupTimer() {
        startupTimer?.invalidate()
        startupTimer = Timer.scheduledTimer(withTimeInterval: startupTimeout, repeats: false) { [weak self] _ in
            self?.fail("Clippie made no startup progress for two minutes. Use Recovery or quit and try again.")
        }
    }

    private func receive(_ bytes: Data) {
        guard !bytes.isEmpty, !stopping else { return }
        if let child = backend, getpgid(child.processIdentifier) == child.processIdentifier {
            ownedGroup = child.processIdentifier
        }
        output.append(bytes)
        // A malformed/noisy backend must not grow memory or disclose its output.
        guard output.count <= 64 * 1024 else {
            fail("Clippie sent an invalid startup response.")
            return
        }
        while let newline = output.firstIndex(of: 0x0A) {
            let line = output.subdata(in: output.startIndex..<newline)
            output.removeSubrange(output.startIndex...newline)
            guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let event = object["event"] as? String else { continue }
            if event == "error" {
                let message = object["message"] as? String ?? "The Clippie backend could not start."
                failedCode = object["code"] as? String
                if maintenance != nil { maintenanceError = message }
                else { fail(message) }
                return
            }
            if event == "startup_progress", editorURL == nil {
                setStatus(object["message"] as? String ?? (maintenance == nil ? "Starting Clippie…" : "Recovery in progress…"))
                resetStartupTimer()
                continue
            }
            if let operation = maintenance {
                if (operation == "info" && event == "recovery_info") || (operation == "restore" && event == "restored") {
                    maintenanceResult = object
                }
                continue
            }
            guard event == "ready", editorURL == nil,
                  let address = object["url"] as? String,
                  let url = URL(string: address),
                  url.scheme == "http", url.host == "127.0.0.1",
                  let port = url.port, port > 0, port <= 65535,
                  url.user == nil, url.password == nil else { continue }
            startupTimer?.invalidate()
            setStatus("Clippie is running")
            bootstrapURL = url
            editorURL = URL(string: "http://127.0.0.1:\(port)/")
            if smoke {
                smokeReady = true
                stopBackend()
            } else {
                openEditor()
            }
        }
    }

    @objc private func openEditor() {
        guard let url = bootstrapURL ?? editorURL, !stopping else { return }
        if !NSWorkspace.shared.open(url) {
            fail("Clippie started, but macOS could not open the editor in your browser.")
        } else {
            // The bootstrap fragment is single-use. Subsequent opens use the
            // authenticated browser session established by the first navigation.
            bootstrapURL = nil
        }
    }

    @objc private func quit() { NSApp.terminate(nil) }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        terminating = true
        stopCompletion = nil
        guard let child = backend, child.isRunning else { return .terminateNow }
        if !stopping { stopBackend() }
        return .terminateLater
    }

    private func stopBackend(then completion: (() -> Void)? = nil) {
        if let completion { stopCompletion = completion }
        guard !stopping else { return }
        stopping = true
        editorURL = nil
        bootstrapURL = nil
        startupTimer?.invalidate()
        guard let child = backend, child.isRunning else {
            finish(status: 0)
            return
        }
        // desktop.py calls setsid before starting workers. Never signal a group
        // unless its ID matches this owned child, including during early startup.
        let pid = child.processIdentifier
        if getpgid(pid) == pid { stoppingGroup = pid }
        else { stoppingGroup = ownedGroup }
        signalOwnedChild(child, signal: SIGTERM)
        shutdownTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: false) { [weak self, weak child] _ in
            guard let self, let child, child.isRunning else { return }
            self.signalOwnedChild(child, signal: SIGKILL)
        }
    }

    private func signalOwnedChild(_ child: Process, signal: Int32) {
        let pid = child.processIdentifier
        if getpgid(pid) == pid {
            _ = kill(-pid, signal)
        } else {
            _ = kill(pid, signal)
        }
    }

    private func backendStopped(status: Int32) {
        startupTimer?.invalidate()
        shutdownTimer?.invalidate()
        // A group leader may exit before workers. Wait for its verified group
        // to disappear before invoking any maintenance continuation.
        if let group = stoppingGroup ?? ownedGroup {
            stoppingGroup = nil
            ownedGroup = nil
            _ = kill(-group, SIGKILL)
            waitForGroupExit(group, status: status, deadline: Date().addingTimeInterval(5))
        } else { completeBackendStop(status: status) }
    }

    private func waitForGroupExit(_ group: pid_t, status: Int32, deadline: Date) {
        if kill(-group, 0) == -1, errno == ESRCH {
            completeBackendStop(status: status)
        } else if Date() >= deadline {
            unconfirmedGroup = group
            stopCompletion = nil
            backend = nil
            stopping = false
            maintenance = nil
            errorShown = false
            fail("Clippie could not confirm that all workers stopped. Recovery is unavailable until they exit. Quit Clippie and try again.")
        } else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
                self?.waitForGroupExit(group, status: status, deadline: deadline)
            }
        }
    }

    private func completeBackendStop(status: Int32) {
        if stopping { finish(status: status) }
        else if let operation = maintenance {
            maintenance = nil
            let result = maintenanceResult
            maintenanceResult = nil
            if status != 0 || result == nil || maintenanceError != nil {
                fail(maintenanceError ?? "Recovery could not complete. Your local data has not been replaced.")
            } else if operation == "info", let result {
                presentRecovery(result)
            } else if let backupID = result?["backup_id"] as? String,
                      backupID == expectedRestoreID,
                      let rescueID = result?["rescue_id"] as? String, validID(rescueID) {
                do { try startBackend() } catch { fail(error.localizedDescription) }
            } else {
                fail("Recovery returned an invalid restore confirmation. Keep Clippie closed and inspect Recovery before continuing.")
            }
        } else if !errorShown {
            fail("Clippie stopped unexpectedly (exit \(status)). Use Recovery or reopen the app.")
        }
    }

    private func fail(_ message: String) {
        guard !errorShown else { return }
        errorShown = true
        startupTimer?.invalidate()
        setStatus("Clippie needs attention")
        if smoke {
            if let child = backend, child.isRunning { stopBackend() }
            else { finish(status: 1) }
            return
        }
        let code = failedCode
        stopBackend { [weak self] in
            guard let self, !self.terminating else { return }
            NSApp.activate(ignoringOtherApps: true)
            let alert = NSAlert()
            alert.messageText = "Clippie could not continue"
            alert.informativeText = String(message.prefix(2000))
            alert.alertStyle = .critical
            alert.addButton(withTitle: "Recovery…")
            if code == "upgrade_interrupted" { alert.addButton(withTitle: "Retry Upgrade") }
            alert.addButton(withTitle: "Keep Closed")
            let choice = alert.runModal()
            if choice == .alertFirstButtonReturn { self.openRecovery() }
            else if code == "upgrade_interrupted", choice == .alertSecondButtonReturn {
                do { try self.startBackend(arguments: ["--retry-upgrade"]) }
                catch { self.errorShown = false; self.fail(error.localizedDescription) }
            }
        }
    }

    @objc private func openRecovery() {
        guard !smoke, !terminating, maintenance == nil else { return }
        stopBackend { [weak self] in
            guard let self else { return }
            do { try self.startBackend(arguments: ["--recovery-info"], operation: "info") }
            catch { self.errorShown = false; self.fail(error.localizedDescription) }
        }
    }

    private func validID(_ value: String) -> Bool {
        value.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil
    }

    private func presentRecovery(_ info: [String: Any]) {
        guard !smoke, !terminating else { return }
        setStatus("Backend stopped — Recovery")
        // IDs are opaque handles. Never consume filesystem paths from the child.
        let snapshots = (info["backups"] as? [[String: Any]] ?? []).filter {
            guard let id = $0["id"] as? String else { return false }
            return validID(id) && ($0["can_restore"] as? Bool == true)
        }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Clippie Recovery"
        alert.informativeText = String((info["message"] as? String ?? "Choose a compatible local snapshot to restore.").prefix(2000))
        let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 430, height: 28), pullsDown: false)
        picker.addItem(withTitle: "Choose a snapshot…")
        for snapshot in snapshots {
            let date = snapshot["created_at"] as? String ?? "Unknown date"
            let version = snapshot["format_version"] as? Int ?? 0
            let id = snapshot["id"] as? String ?? ""
            picker.addItem(withTitle: "\(date) — format \(version) — \(id.prefix(8))")
        }
        alert.accessoryView = picker
        if !snapshots.isEmpty { alert.addButton(withTitle: "Restore…") }
        else { alert.informativeText += "\nNo compatible snapshots are available." }
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn, !snapshots.isEmpty,
              picker.indexOfSelectedItem > 0 else { return }
        let snapshot = snapshots[picker.indexOfSelectedItem - 1]
        guard let id = snapshot["id"] as? String, validID(id) else { return }
        let confirmation = NSAlert()
        confirmation.alertStyle = .warning
        confirmation.messageText = "Restore snapshot from \(snapshot["created_at"] as? String ?? "unknown date")?"
        confirmation.informativeText = "This restores the whole Clippie data folder, including projects, media and models inside it, to this snapshot. Clippie retains a full rescue copy of the current state. Files referenced outside this folder and Keychain credentials are not backed up or reverted. Remote publishing and costs cannot be undone. Clippie will restart after restoring; review projects before resuming work."
        confirmation.addButton(withTitle: "Cancel")
        confirmation.addButton(withTitle: "Restore Snapshot")
        guard confirmation.runModal() == .alertSecondButtonReturn else { return }
        do { try startBackend(arguments: ["--restore-backup", id], operation: "restore") }
        catch { errorShown = false; fail(error.localizedDescription) }
    }

    private func finish(status: Int32) {
        if let directory = smokeDirectory { try? FileManager.default.removeItem(at: directory) }
        if smoke {
            let orderly = status == 0 || status == SIGTERM
            let result: [String: Any] = ["event": "launcher_smoke", "ready": smokeReady,
                                          "backend_exit": status, "success": smokeReady && !errorShown && orderly]
            if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]) {
                FileHandle.standardOutput.write(data)
                FileHandle.standardOutput.write(Data([0x0A]))
            }
            exit(smokeReady && !errorShown && orderly ? 0 : 1)
        }
        backend = nil
        maintenance = nil
        stopping = false
        let completion = stopCompletion
        stopCompletion = nil
        if terminating { NSApp.reply(toApplicationShouldTerminate: true) }
        else { completion?() }
    }

    private func startupError(_ message: String) -> NSError {
        NSError(domain: "ClippieLauncher", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}

let application = NSApplication.shared
let delegate = ClippieDelegate()
application.delegate = delegate
application.run()

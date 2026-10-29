import AppKit
import Foundation
import PorteKit
import UniformTypeIdentifiers

struct AppAlert: Identifiable {
    let id = UUID()
    var title: String
    var message: String
    var primaryTitle: String = "OK"
    var primaryAction: (() -> Void)?
    var secondaryTitle: String?
    var secondaryAction: (() -> Void)?
    var destructive: Bool = false
}

/// One analysis / install / run session, shown in a sheet.
struct Workflow: Identifiable, Equatable {
    let id: UUID
    let fileURL: URL
    var state: WorkflowState = .analyzing
    var message: String = "Inspecting the file…"
    var report: AnalysisReport?
    var installResult: InstallResult?
    var runResult: RunResult?
    var log: [String] = []
    var errorMessage: String?
    var errorCode: String?
    var canRetryWithForce = false

    var isBusy: Bool { state.isBusy }
    var canDismiss: Bool { !state.isBusy }
    var displayName: String { report?.name ?? fileURL.deletingPathExtension().lastPathComponent }
}

@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    enum Phase: Equatable { case launching, setup, ready }

    @Published private(set) var phase: Phase = .launching
    @Published private(set) var runtime: RuntimeDescriptor?
    @Published private(set) var hello: HelloInfo?
    @Published private(set) var backends: [RuntimeBackendStatus] = []
    @Published private(set) var compatibilityReport: [String] = []
    @Published private(set) var library = LibrarySnapshot()
    @Published private(set) var associations: [AssociationStatus] = []
    @Published private(set) var isRefreshing = false
    @Published private(set) var runtimeError: String?
    @Published private(set) var setupState = SetupState()
    @Published private(set) var diagnostics: DiagnosticsReport?
    @Published private(set) var diagnosticsText: String?
    @Published var workflow: Workflow?
    @Published var alert: AppAlert?
    @Published var searchText = ""
    @Published var detailApp: InstalledApplication?
    @Published var detailProduct: InstalledProduct?
    @Published var installationDestination: URL
    @Published var selectedFileTypeNote: String?
    @Published private(set) var installedAppsCount: Int = 0

    private let store: SetupStore
    private let events: EventLog
    private let openQueue = FileOpenQueue()
    private var bridge: PorteBridge?
    private var pendingOpenURLs: [URL] = []
    /// Files that arrived while another workflow sheet was open. Each is handled
    /// once the current one is dismissed, so nothing Finder delivered is lost.
    private var queuedOpenURLs: [URL] = []
    private var prepared = false
    private let defaults = UserDefaults.standard
    private static let destinationKey = "PorteInstallationDirectory"

    private init(store: SetupStore = SetupStore()) {
        self.store = store
        self.events = EventLog(url: store.eventsURL)
        let saved = defaults.string(forKey: Self.destinationKey)
        self.installationDestination = saved.map {
            URL(fileURLWithPath: $0, isDirectory: true)
        } ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Applications", isDirectory: true)
    }

    // MARK: - Startup

    var appVersion: String {
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String)
            ?? hello?.version
            ?? "0.1.0"
    }

    var canFinishSetup: Bool {
        guard let hello else { return false }
        return hello.health.engine && hello.health.writable
    }

    /// The backend that runs real third-party Windows GUI programs.
    var compatibilityRuntime: RuntimeBackendStatus? {
        backends.first { $0.isCompatibilityRuntime }
    }

    var compatibilityRuntimeDetail: String {
        guard let status = compatibilityRuntime else {
            return "No compatibility runtime has been reported yet."
        }
        if status.available {
            let version = status.version.map { " \($0)" } ?? ""
            return "Wine\(version) at \(status.path ?? "an unknown location")"
        }
        return status.reasons.first ?? "No Wine runtime was found on this Mac."
    }

    var compatibilityRuntimeReady: Bool { compatibilityRuntime?.available == true }

    var runtimeDetail: String {
        if let runtimeError { return runtimeError }
        guard let hello, let runtime else {
            return "Porte's execution runtime was not found."
        }
        var parts = ["Python at \(runtime.python)"]
        if let version = runtime.version { parts.append("Porte \(version)") }
        if !hello.health.ok { parts.append(hello.health.summary) }
        return parts.joined(separator: " · ")
    }

    func prepare() async {
        guard !prepared else { return }
        prepared = true
        setupState = store.load()
        if let value = defaults.string(forKey: Self.destinationKey) {
            installationDestination = URL(fileURLWithPath: value, isDirectory: true)
        }
        events.record("launch", [
            "version": .string(appVersion),
            "bundle": .string(Bundle.main.bundleURL.path),
        ])

        guard let discovered = RuntimeLocator.discover(store: store),
              discovered.isValid else {
            runtimeError = SetupRuntimeMessage.missing
            phase = .setup
            updateAssociations()
            events.record("runtime_error", ["message": .string(SetupRuntimeMessage.missing)])
            return
        }
        runtime = discovered
        await connect()
    }

    private func connect() async {
        do {
            let info = try await helloInfo()
            hello = info
            backends = info.backends ?? []
            runtimeError = nil
            updateAssociations()
            events.record("runtime_ready", [
                "python": .string(info.python),
                "version": .string(info.version),
                "health": .bool(info.health.ok),
            ])
            if setupState.isComplete && info.health.ok {
                phase = .ready
                refreshLibrary()
                await processPendingOpens()
            } else {
                phase = .setup
            }
        } catch {
            runtimeError = error.localizedDescription
            phase = .setup
            updateAssociations()
            events.record("runtime_error", ["message": .string(error.localizedDescription)])
        }
    }

    func recheckRuntime() {
        bridge = nil
        Task { await connect() }
    }

    private func helloInfo() async throws -> HelloInfo {
        let bridge = try await requireBridge()
        let document = try await bridge.request("hello")
        return try document.decoded(HelloInfo.self)
    }

    private func requireBridge() async throws -> PorteBridge {
        if let bridge { return bridge }
        guard let runtime else {
            throw RuntimeSelectionError(SetupRuntimeMessage.missing)
        }
        var environment: [String: String] = ["PORTE_GUI": "1"]
        if let chosen = setupState.compatibilityRuntimePath, !chosen.isEmpty {
            environment["PORTE_WINE_ROOT"] = chosen
        }
        let bridge = PorteBridge(runtime: runtime, environment: environment)
        _ = try await bridge.start()
        self.bridge = bridge
        return bridge
    }

    // MARK: - Compatibility runtime

    /// Point Porte at a Wine runtime the user installed themselves.
    ///
    /// Porte deliberately does not download or install one: the picker records
    /// a path, and the engine verifies the runtime before it is used.
    func chooseCompatibilityRuntime() {
        let panel = NSOpenPanel()
        panel.title = "Choose a Wine Runtime"
        panel.message = """
        Select a Wine application (for example “Wine Staging.app”) or its wine \
        binary. Porte uses the runtime you already installed; it never downloads \
        or copies one.
        """
        panel.prompt = "Use Runtime"
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        var state = setupState
        state.compatibilityRuntimePath = url.path
        try? store.save(state)
        setupState = state
        bridge = nil
        compatibilityReport = []
        events.record("compatibility_runtime_selected", [
            "path": .string(url.path),
        ])
        Task { await connect() }
    }

    /// Ask the engine to verify the runtime and execute a harmless guest
    /// program; the report is real output, not a progress animation.
    func verifyCompatibilityRuntime() {
        Task {
            do {
                let bridge = try await requireBridge()
                let document = try await bridge.request("runtime", args: [
                    "action": .string("verify"),
                    "backend": .string("wine"),
                    "smoke": .bool(true),
                ])
                var lines: [String] = []
                if let data = document.objectValue,
                   let checks = data["checks"]?.arrayValue {
                    for check in checks {
                        let name = check.objectValue?["check"]?.stringValue ?? "check"
                        let ok = check.objectValue?["ok"]?.boolValue ?? false
                        let detail = check.objectValue?["detail"]?.stringValue ?? ""
                        lines.append("\(ok ? "ok" : "failed")  \(name): \(detail)")
                    }
                }
                compatibilityReport = lines.isEmpty
                    ? ["The engine returned no checks."] : lines
                events.record("compatibility_verified", [
                    "ok": .bool(compatibilityRuntimeReady),
                ])
            } catch {
                compatibilityReport = [error.localizedDescription]
            }
        }
    }

    // MARK: - Setup

    func chooseRuntime() {
        let panel = NSOpenPanel()
        panel.title = "Choose the Porte Runtime"
        panel.message = """
        Select the Python interpreter from a Porte environment (for example \
        .venv/bin/python), the `porte` command, or a checkout that contains \
        .venv.
        """
        panel.prompt = "Use Runtime"
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task {
            do {
                let descriptor = try await Task.detached(priority: .userInitiated) {
                    try RuntimeLocator.descriptor(forSelection: url)
                }.value
                try store.save(runtime: descriptor)
                runtime = descriptor
                setupState.runtime = descriptor
                bridge = nil
                events.record("runtime_selected", ["python": .string(descriptor.python)])
                await connect()
            } catch {
                alert = AppAlert(title: "That runtime cannot be used",
                                 message: error.localizedDescription)
            }
        }
    }

    func completeSetup() {
        guard canFinishSetup, let runtime else { return }
        var state = setupState
        state.completedAt = Date()
        state.runtime = runtime
        do {
            try store.save(state)
        } catch {
            alert = AppAlert(title: "Setup could not be saved",
                             message: error.localizedDescription)
            return
        }
        setupState = state
        phase = .ready
        events.record("setup_completed", ["python": .string(runtime.python)])
        refreshLibrary()
        Task { await processPendingOpens() }
    }

    func resetSetup() {
        try? store.clear()
        setupState = SetupState()
        phase = .setup
        workflow = nil
    }

    func showSetup() {
        if phase == .ready { phase = .setup }
    }

    // MARK: - File associations

    func updateAssociations() {
        associations = FileAssociations.allStatuses()
    }

    func makePorteDefault(for type: PorteFileType? = nil) {
        let failures = FileAssociations.makePorteDefault(
            type.map { [$0] } ?? PorteFileType.allCases)
        updateAssociations()
        var state = setupState
        if failures.isEmpty {
            state.fileAssociations = "set"
            events.record("associations_set")
            try? store.save(state)
            setupState = state
        } else {
            state.fileAssociations = "unavailable"
            try? store.save(state)
            setupState = state
            let details = failures.map { type, error in
                "\(type.displayName): \(error.localizedDescription)"
            }.joined(separator: "\n")
            alert = AppAlert(title: "macOS needs your approval",
                             message: "\(FileAssociations.manualInstructions)\n\n\(details)")
        }
    }

    // MARK: - Opening files

    func open(urls: [URL]) {
        let accepted = openQueue.accept(urls)
        guard !accepted.isEmpty else { return }
        for url in accepted {
            events.record("open_received", ["path": .string(url.path)])
        }
        let windowsFiles = accepted.filter(FileAssociations.isWindowsProgram)
        let rejected = accepted.filter { !FileAssociations.isWindowsProgram($0) }
        if !rejected.isEmpty {
            alert = AppAlert(
                title: "Porte opens Windows programs",
                message: "Porte can open .exe and .msi files (and .msix or .appx packages). "
                    + rejected.map(\.lastPathComponent).joined(separator: ", ")
                    + " is not one of them.")
        }
        guard !windowsFiles.isEmpty else { return }
        if phase != .ready {
            pendingOpenURLs.append(contentsOf: windowsFiles)
            return
        }
        enqueueWorkflows(windowsFiles)
    }

    private func enqueueWorkflows(_ urls: [URL]) {
        guard let first = urls.first else { return }
        if workflow != nil {
            queuedOpenURLs.append(contentsOf: urls)
            return
        }
        queuedOpenURLs.append(contentsOf: urls.dropFirst())
        Task { await beginWorkflow(url: first) }
    }

    private func processPendingOpens() async {
        guard phase == .ready else { return }
        let queued = pendingOpenURLs
        pendingOpenURLs = []
        enqueueWorkflows(queued)
    }

    func chooseFile() {
        let panel = NSOpenPanel()
        panel.title = "Open a Windows App"
        panel.prompt = "Open"
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        var types: [UTType] = []
        for type in PorteFileType.allCases {
            types.append(type.contentType)
        }
        if let msix = UTType(filenameExtension: "msix") { types.append(msix) }
        if let appx = UTType(filenameExtension: "appx") { types.append(appx) }
        panel.allowedContentTypes = types
        panel.allowsOtherFileTypes = true
        guard panel.runModal() == .OK else { return }
        open(urls: panel.urls)
    }

    private func beginWorkflow(url: URL) async {
        let id = UUID()
        workflow = Workflow(id: id, fileURL: url)
        events.record("analysis_started", ["path": .string(url.path)])
        do {
            let bridge = try await requireBridge()
            let document = try await bridge.request(
                "analyze", args: ["path": .string(url.path)]
            ) { event in
                Task { @MainActor in self.apply(event, to: id) }
            }
            let report = try document.decoded(AnalysisReport.self)
            guard var current = workflow, current.id == id,
                  current.state != .cancelled else { return }
            current.report = report
            current.state = report.supported ? .completed : .unsupported
            current.message = report.summary
            workflow = current
            events.record("analysis_result", [
                "path": .string(url.path),
                "kind": .string(report.kind),
                "supported": .bool(report.supported),
            ])
        } catch {
            failWorkflow(error, id: id)
        }
    }

    private func apply(_ event: BridgeEvent, to id: UUID) {
        guard var current = workflow, current.id == id else { return }
        if let state = event.state, !state.isEmpty {
            let mapped = WorkflowState(bridgeState: state)
            if mapped != .idle { current.state = mapped }
        }
        if let message = event.message, !message.isEmpty {
            current.message = message
            if event.event == "log" {
                current.log.append(message)
            }
        }
        workflow = current
    }

    private func failWorkflow(_ error: Error, id: UUID) {
        guard var current = workflow, current.id == id else { return }
        if let failure = error as? BridgeFailure {
            current.errorCode = failure.code
            current.errorMessage = failure.message
            current.canRetryWithForce = failure.code == "already_installed"
            current.state = failure.code == "unsupported" ? .unsupported
                : (failure.code == "cancelled" ? .cancelled : .failed)
        } else {
            current.errorMessage = error.localizedDescription
            current.state = .failed
        }
        current.message = current.errorMessage ?? "The operation failed."
        workflow = current
        events.record("workflow_error", [
            "id": .string(id.uuidString),
            "code": .string(current.errorCode ?? "error"),
            "message": .string(current.message),
        ])
    }

    // MARK: - Install and run

    func installCurrentWorkflow(force: Bool = false) {
        guard let current = workflow, let report = current.report,
              report.canInstall, !current.isBusy else { return }
        let id = current.id
        let path = current.fileURL.path
        workflow?.state = .preparing
        workflow?.message = "Preparing…"
        workflow?.log = []
        workflow?.errorMessage = nil
        workflow?.errorCode = nil
        workflow?.canRetryWithForce = false
        events.record("install_started", ["path": .string(path),
                                          "destination": .string(installationDestination.path)])
        Task {
            do {
                try FileManager.default.createDirectory(at: installationDestination,
                                                        withIntermediateDirectories: true)
                let bridge = try await requireBridge()
                let args: [String: JSONValue] = [
                    "path": .string(path),
                    "consent": .bool(true),
                    "dest": .string(installationDestination.path),
                    "force": .bool(force),
                ]
                let document = try await bridge.request("install", args: args) { event in
                    Task { @MainActor in self.apply(event, to: id) }
                }
                let result = try document.decoded(InstallResult.self)
                guard var current = workflow, current.id == id,
                      current.state != .cancelled else { return }
                current.installResult = result
                current.state = .completed
                current.message = result.bundles.isEmpty
                    ? "The package was installed, but no launchable application was found."
                    : "Installed. The application is now in Applications."
                workflow = current
                events.record("install_result", [
                    "path": .string(path),
                    "product": .string(result.product ?? ""),
                    "apps": .array(result.bundles.map { .string($0.app) }),
                ])
                refreshLibrary()
            } catch {
                failWorkflow(error, id: id)
            }
        }
    }

    func runCurrentWorkflow() {
        guard let current = workflow, let report = current.report,
              report.canRunNow, !current.isBusy else { return }
        let id = current.id
        let path = current.fileURL.path
        workflow?.state = .running
        workflow?.message = "Running…"
        workflow?.runResult = nil
        events.record("run_started", ["path": .string(path)])
        Task {
            do {
                let bridge = try await requireBridge()
                let document = try await bridge.request(
                    "run", args: ["path": .string(path), "consent": .bool(true)]
                ) { event in
                    Task { @MainActor in self.apply(event, to: id) }
                }
                let result = try document.decoded(RunResult.self)
                guard var current = workflow, current.id == id,
                      current.state != .cancelled else { return }
                current.runResult = result
                current.state = .completed
                current.message = result.fault == nil
                    ? "The program exited with status \(result.exitCode)."
                    : "The program faulted: \(result.fault ?? "unknown fault")"
                workflow = current
                events.record("run_result", [
                    "path": .string(path),
                    "exit": .number(Double(result.exitCode)),
                ])
            } catch {
                failWorkflow(error, id: id)
            }
        }
    }

    func cancelWorkflow() {
        guard let current = workflow, current.isBusy else { return }
        Task { await bridge?.cancel() }
        workflow?.state = .cancelled
        workflow?.message = "Cancelling…"
        events.record("cancel_requested", ["id": .string(current.id.uuidString)])
    }

    func dismissWorkflow() {
        guard workflow?.canDismiss == true else { return }
        workflow = nil
        if !queuedOpenURLs.isEmpty {
            let next = queuedOpenURLs.removeFirst()
            Task { await beginWorkflow(url: next) }
        }
    }

    // MARK: - Library

    func refreshLibrary() {
        guard phase == .ready else { return }
        isRefreshing = true
        Task {
            defer { isRefreshing = false }
            do {
                let bridge = try await requireBridge()
                let document = try await bridge.request(
                    "library", args: ["dest": .string(installationDestination.path)])
                let snapshot = try document.decoded(LibrarySnapshot.self)
                library = snapshot
                installedAppsCount = snapshot.applications.count
            } catch {
                alert = AppAlert(title: "Could not load the library",
                                 message: error.localizedDescription)
            }
        }
    }

    var filteredApplications: [InstalledApplication] {
        let query = searchText.trimmingCharacters(in: .whitespaces)
        guard !query.isEmpty else { return library.applications }
        return library.applications.filter {
            $0.name.localizedCaseInsensitiveContains(query)
        }
    }

    func product(for app: InstalledApplication) -> InstalledProduct? {
        guard let key = app.productKey else { return nil }
        return library.products.first { $0.productKey == key }
    }

    func product(forBundlePath path: String) -> InstalledProduct? {
        library.products.first { product in
            product.bundles.contains { $0.app == path }
        }
    }

    func launch(_ app: InstalledApplication) {
        guard app.isReady else {
            alert = AppAlert(title: "\(app.name) cannot start",
                             message: app.problem ?? "The application bundle is incomplete.")
            return
        }
        events.record("launch_app", ["path": .string(app.path)])
        let configuration = NSWorkspace.OpenConfiguration()
        NSWorkspace.shared.openApplication(at: app.appURL,
                                           configuration: configuration) { [weak self] _, error in
            guard let error else { return }
            Task { @MainActor in
                self?.alert = AppAlert(title: "Could not open \(app.name)",
                                       message: error.localizedDescription)
            }
        }
    }

    func reveal(_ app: InstalledApplication) {
        NSWorkspace.shared.activateFileViewerSelecting([app.appURL])
    }

    /// Open a bundle that was just generated, before the library refreshes.
    func launchBundle(at path: String) {
        events.record("launch_app", ["path": .string(path)])
        NSWorkspace.shared.openApplication(
            at: URL(fileURLWithPath: path),
            configuration: NSWorkspace.OpenConfiguration()
        ) { [weak self] _, error in
            guard let error else { return }
            Task { @MainActor in
                self?.alert = AppAlert(title: "Could not open the application",
                                       message: error.localizedDescription)
            }
        }
    }

    func reveal(path: String) {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func showDetails(_ app: InstalledApplication) {
        detailApp = app
        detailProduct = product(for: app)
    }

    func uninstall(_ app: InstalledApplication) {
        let product = product(for: app)
        guard let product else {
            confirmUninstall(title: "Remove “\(app.name)”?",
                             message: "The application bundle and its Porte data will be deleted.",
                             target: .bundle(app))
            return
        }
        let others = product.bundles.filter { $0.app != app.path }
        confirmUninstall(
            title: "Uninstall “\(product.name)”?",
            message: others.isEmpty
                ? "The installed Windows program, its environment and its application will be deleted."
                : "This removes the whole Windows product, including \(others.count) other application(s).",
            target: .product(product))
    }

    private enum UninstallTarget {
        case bundle(InstalledApplication)
        case product(InstalledProduct)
    }

    private func confirmUninstall(title: String, message: String, target: UninstallTarget) {
        alert = AppAlert(
            title: title,
            message: message,
            primaryTitle: "Uninstall",
            primaryAction: { [weak self] in self?.performUninstall(target) },
            secondaryTitle: "Cancel",
            destructive: true)
    }

    private func performUninstall(_ target: UninstallTarget) {
        Task {
            do {
                let bridge = try await requireBridge()
                var args: [String: JSONValue] = [:]
                switch target {
                case .bundle(let app):
                    args = [
                        "target": .string("bundle"),
                        "id": .string(app.bundleId),
                        "path": .string(app.path),
                    ]
                case .product(let product):
                    args = [
                        "target": .string("product"),
                        "id": .string(product.productKey),
                    ]
                }
                _ = try await bridge.request("uninstall", args: args)
                detailApp = nil
                detailProduct = nil
                refreshLibrary()
                events.record("uninstall_finished", ["args": .object(args)])
            } catch {
                alert = AppAlert(title: "Uninstall failed",
                                 message: error.localizedDescription)
            }
        }
    }

    func repair(_ product: InstalledProduct) {
        Task {
            do {
                let bridge = try await requireBridge()
                _ = try await bridge.request("repair", args: ["product": .string(product.productKey)])
                refreshLibrary()
                alert = AppAlert(title: "Repair complete",
                                 message: "\(product.name) was verified and restored.")
            } catch {
                alert = AppAlert(title: "Repair failed",
                                 message: error.localizedDescription)
            }
        }
    }

    func openProductEnvironment(_ product: InstalledProduct) {
        guard let environment = product.environment else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: environment)])
    }

    // MARK: - Settings and diagnostics

    func chooseInstallationDestination() {
        let panel = NSOpenPanel()
        panel.title = "Choose Where Applications Are Installed"
        panel.prompt = "Choose"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.directoryURL = installationDestination
        guard panel.runModal() == .OK, let url = panel.url else { return }
        installationDestination = url
        defaults.set(url.path, forKey: Self.destinationKey)
        events.record("destination_changed", ["path": .string(url.path)])
        refreshLibrary()
    }

    func useStandardDestination() {
        let home = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Applications", isDirectory: true)
        installationDestination = home
        defaults.set(home.path, forKey: Self.destinationKey)
        refreshLibrary()
    }

    func loadDiagnostics(force: Bool = false) async {
        guard phase == .ready else { return }
        if diagnostics != nil && !force { return }
        do {
            let bridge = try await requireBridge()
            let document = try await bridge.request("diagnostics")
            let report = try document.decoded(DiagnosticsReport.self)
            diagnostics = report
            diagnosticsText = renderDiagnostics(report, bridge: await bridge.diagnostics())
        } catch {
            diagnosticsText = "Diagnostics unavailable: \(error.localizedDescription)"
        }
    }

    private func renderDiagnostics(_ report: DiagnosticsReport, bridge: String) -> String {
        var lines: [String] = []
        lines.append("Porte \(appVersion)")
        lines.append("bridge protocol \(report.protocolVersion)")
        lines.append("engine version \(report.version)")
        lines.append("python \(report.python)")
        lines.append("package root \(report.root)")
        lines.append("support root \(report.supportRoot)")
        lines.append("applications root \(report.applicationsRoot)")
        lines.append("installation destination \(installationDestination.path)")
        lines.append("runtime health: \(report.health.ok ? "ok" : "problems")")
        for problem in report.health.problems {
            lines.append("  - \(problem)")
        }
        lines.append("clang: \(report.toolchain?.clang ?? "unknown")")
        lines.append("cache bytes: \(report.cacheBytes)")
        lines.append("installed products: \(report.products.count)")
        if let count = report.host["chip"]?.stringValue { lines.append("host: \(count)") }
        if let os = report.host["os"]?.stringValue { lines.append("macOS \(os)") }
        lines.append("")
        lines.append(bridge)
        return lines.joined(separator: "\n")
    }

    func copyDiagnosticsToPasteboard() {
        Task {
            await loadDiagnostics(force: true)
            let text = diagnosticsText ?? "No diagnostics available."
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        }
    }

    func clearCaches() {
        Task {
            do {
                let bridge = try await requireBridge()
                let document = try await bridge.request("clear_caches")
                let freed = document["freed_bytes"]?.intValue ?? 0
                await loadDiagnostics(force: true)
                alert = AppAlert(
                    title: "Caches cleared",
                    message: freed > 0
                        ? "Freed \(ByteCountFormatter.string(fromByteCount: Int64(freed), countStyle: .file)). Applications rebuild their caches on next launch."
                        : "There was no cached data to remove.")
                events.record("caches_cleared", ["bytes": .number(Double(freed))])
            } catch {
                alert = AppAlert(title: "Could not clear caches",
                                 message: error.localizedDescription)
            }
        }
    }

    func openSupportFolder() {
        try? FileManager.default.createDirectory(at: store.baseDirectory,
                                                 withIntermediateDirectories: true)
        NSWorkspace.shared.open(store.baseDirectory)
    }

    func openLicenses() {
        if let runtime {
            let path = runtime.rootURL.appendingPathComponent("THIRD_PARTY.md")
            if FileManager.default.fileExists(atPath: path.path) {
                NSWorkspace.shared.open(path)
                return
            }
        }
        if let url = URL(string: "https://github.com/notomer/porte/blob/main/THIRD_PARTY.md") {
            NSWorkspace.shared.open(url)
        }
    }

    func openRepository() {
        if let url = URL(string: "https://github.com/notomer/porte") {
            NSWorkspace.shared.open(url)
        }
    }

    func showWindow() {
        NSApp.activate(ignoringOtherApps: true)
        if let window = NSApp.windows.first(where: { $0.canBecomeMain }) {
            window.makeKeyAndOrderFront(nil)
        }
    }
}

enum SetupRuntimeMessage {
    static let missing = "Porte's execution runtime was not found on this Mac. "
        + "Choose the Python interpreter from a Porte environment to continue."
}

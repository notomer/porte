import AppKit
import PorteKit
import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        TabView {
            GeneralSettings()
                .tabItem { Label("General", systemImage: "gearshape") }
            CompatibilitySettings()
                .tabItem { Label("Compatibility", systemImage: "puzzlepiece.extension") }
            PerformanceSettings()
                .tabItem { Label("Performance", systemImage: "speedometer") }
            AboutSettings()
                .tabItem { Label("About", systemImage: "info.circle") }
        }
        .padding(.top, 6)
        .task { await model.loadDiagnostics() }
        .porteAlert($model.alert)
    }
}

// MARK: - General

private struct GeneralSettings: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Form {
            Section {
                LabeledContent("Install applications in") {
                    HStack(spacing: 8) {
                        Text(model.installationDestination.path)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .foregroundStyle(.secondary)
                        Button("Choose…") { model.chooseInstallationDestination() }
                    }
                }
            } header: {
                Text("Applications")
            } footer: {
                Text("Porte creates a macOS application for each launchable program. "
                     + "The default is ~/Applications, which needs no administrator "
                     + "privileges. Choosing /Applications requires your approval.")
                    .font(.caption)
            }

            Section {
                ForEach(model.associations, id: \.type.rawValue) { status in
                    LabeledContent(status.type.displayName) {
                        HStack(spacing: 8) {
                            Text(status.summary)
                                .foregroundStyle(.secondary)
                            if status.isPorte {
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundStyle(.green)
                                    .accessibilityLabel("Porte is the default")
                            } else {
                                Button("Make Default") {
                                    model.makePorteDefault(for: status.type)
                                }
                            }
                        }
                    }
                }
            } header: {
                Text("File Associations")
            } footer: {
                Text(FileAssociations.manualInstructions)
                    .font(.caption)
            }

            Section {
                LabeledContent("Setup") {
                    HStack(spacing: 8) {
                        Text(model.setupState.isComplete ? "Completed" : "Required")
                            .foregroundStyle(.secondary)
                        Button("Run Setup Again") { model.resetSetup() }
                    }
                }
            } header: {
                Text("Setup")
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - Compatibility

private struct CompatibilitySettings: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var advanced = ViewFlag()

    private var health: HealthReport? { model.hello?.health }

    var body: some View {
        Form {
            Section {
                LabeledContent("Runtime") {
                    HStack(spacing: 6) {
                        Circle()
                            .fill(health?.ok == true ? Color.green : Color.orange)
                            .frame(width: 8, height: 8)
                        Text(health?.ok == true ? "Ready" : "Needs attention")
                    }
                }
                LabeledContent("Porte version", value: model.hello?.version ?? "—")
                LabeledContent("Python", value: model.runtime?.python ?? "—")
                HealthRow(title: "Execution engine", ok: health?.engine == true)
                HealthRow(title: "Application builder (clang)", ok: health?.clang == true)
                HealthRow(title: "Icon toolchain", ok: health?.iconutil == true)
                HealthRow(title: "Support folder writable", ok: health?.writable == true)
                if let problems = health?.problems, !problems.isEmpty {
                    ForEach(problems, id: \.self) { problem in
                        Label(problem, systemImage: "exclamationmark.triangle")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
                Button("Check Again") { model.recheckRuntime() }
            } header: {
                Text("Compatibility Runtime")
            }

            Section {
                ForEach(model.backends) { backend in
                    LabeledContent(backend.name) {
                        HStack(spacing: 6) {
                            Circle()
                                .fill(backend.available ? Color.green : Color.orange)
                                .frame(width: 8, height: 8)
                            Text(backend.available
                                 ? (backend.version.map { "Ready · \($0)" } ?? "Ready")
                                 : "Unavailable")
                        }
                    }
                    if let path = backend.path {
                        LabeledContent("Location", value: path)
                    }
                    Text(backend.license)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    ForEach(backend.reasons, id: \.self) { reason in
                        Text(reason)
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                    ForEach(backend.warnings, id: \.self) { warning in
                        Text(warning)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                HStack(spacing: 8) {
                    Button("Verify Runtime") { model.verifyCompatibilityRuntime() }
                    Button("Choose Wine Runtime…") { model.chooseCompatibilityRuntime() }
                }
                if !model.compatibilityReport.isEmpty {
                    ForEach(model.compatibilityReport, id: \.self) { line in
                        Text(line)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
                Text("Porte does not download or install a compatibility runtime. "
                     + "It runs the Wine distribution already on this Mac and "
                     + "records its version with every installed application.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                Text("Execution Backends")
            }

            Section {
                LabeledContent("Default profile", value: "Automatic")
                Text("Porte picks a profile per executable from the Windows APIs it "
                     + "imports: console or Metal frame. It is not a user setting "
                     + "because the engine, not the user, decides what it can run.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                Text("Compatibility")
            }

            Section {
                DisclosureGroup(isExpanded: $advanced.value) {
                    VStack(alignment: .leading, spacing: 6) {
                        LabeledContent("Package root",
                                       value: model.runtime?.root ?? "—")
                        LabeledContent("Support folder") {
                            Button("Show in Finder") { model.openSupportFolder() }
                        }
                        Button("Copy Diagnostics") { model.copyDiagnosticsToPasteboard() }
                    }
                    .padding(.top, 4)
                } label: {
                    Text("Advanced")
                }
            }
        }
        .formStyle(.grouped)
    }
}

private struct HealthRow: View {
    let title: String
    let ok: Bool

    var body: some View {
        LabeledContent(title) {
            Image(systemName: ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(ok ? Color.green : Color.orange)
                .accessibilityLabel(ok ? "Available" : "Unavailable")
        }
    }
}

// MARK: - Performance

private struct PerformanceSettings: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var diagnostics = ViewFlag()

    var body: some View {
        Form {
            Section {
                LabeledContent("Translation cache") {
                    Text(Formatters.bytes(model.diagnostics?.cacheBytes ?? 0))
                        .foregroundStyle(.secondary)
                }
                Button("Clear Application Caches") { model.clearCaches() }
            } header: {
                Text("Cache")
            } footer: {
                Text("Caches hold per-application analysis results. Clearing them is "
                     + "safe; the engine rebuilds them the next time an application runs.")
                    .font(.caption)
            }

            Section {
                LabeledContent("Frame renderer", value: "Offscreen Metal")
                Text("The frame profile runs one Metal scene offscreen. The engine has "
                     + "no configurable graphics backends yet, so Porte does not offer "
                     + "graphics toggles that would do nothing.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                Text("Graphics")
            }

            Section {
                DisclosureGroup(isExpanded: $diagnostics.value) {
                    VStack(alignment: .leading, spacing: 8) {
                        ScrollView {
                            Text(model.diagnosticsText ?? "Collecting diagnostics…")
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .frame(height: 150)
                        HStack {
                            Button("Copy Diagnostics") { model.copyDiagnosticsToPasteboard() }
                            Button("Refresh") {
                                Task { await model.loadDiagnostics(force: true) }
                            }
                        }
                    }
                    .padding(.top, 4)
                } label: {
                    Text("Diagnostics")
                }
            } header: {
                Text("Diagnostics")
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - About

private struct AboutSettings: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 10) {
            AppIconView(path: Bundle.main.bundlePath, size: 72)
                .padding(.top, 12)
            Text("Porte")
                .font(.title2.weight(.semibold))
            Text("Your Windows apps. At home on Mac.")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text("Version \(model.appVersion)")
                .font(.callout)
                .foregroundStyle(.secondary)

            Divider().padding(.vertical, 6)

            HStack(spacing: 12) {
                Button("GitHub Repository") { model.openRepository() }
                Button("Open-Source Licenses") { model.openLicenses() }
            }

            if let runtime = model.runtime {
                Text(runtime.root)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .padding(.horizontal, 24)
            }

            Text("Porte is an independent open-source project and is not affiliated "
                 + "with Microsoft Corporation. Windows is a trademark of Microsoft "
                 + "Corporation.")
                .font(.caption)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 36)
                .padding(.top, 4)

            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

import AppKit
import PorteKit
import SwiftUI

struct WorkflowSheet: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var technical = ViewFlag()

    var body: some View {
        if let workflow = model.workflow {
            VStack(alignment: .leading, spacing: 0) {
                header(workflow)
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        Text(workflow.message)
                            .font(.body)
                            .foregroundStyle(workflow.state.isFailure ? Color.secondary : Color.primary)
                            .fixedSize(horizontal: false, vertical: true)
                        if let report = workflow.report {
                            reportBlock(report)
                        }
                        if let result = workflow.installResult {
                            installBlock(result)
                        }
                        if let run = workflow.runResult {
                            runBlock(run)
                        }
                        if let error = workflow.errorMessage {
                            errorBlock(error, workflow: workflow)
                        }
                        technicalBlock(workflow)
                    }
                    .padding(20)
                }
                Divider()
                footer(workflow)
            }
            .frame(width: 600, height: 520)
            .interactiveDismissDisabled(workflow.isBusy)
        }
    }

    // MARK: - Header

    private func header(_ workflow: Workflow) -> some View {
        HStack(spacing: 14) {
            PEIconView(base64: workflow.report?.iconPng, size: 44)
            VStack(alignment: .leading, spacing: 2) {
                Text(workflow.displayName)
                    .font(.title3.weight(.semibold))
                    .lineLimit(1)
                Text(workflow.report?.kindLabel
                     ?? workflow.fileURL.pathExtension.uppercased())
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            if workflow.isBusy {
                ProgressView()
                    .controlSize(.small)
            }
            StatusBadge(text: workflow.state.title, tint: tint(for: workflow.state))
        }
        .padding(20)
    }

    private func tint(for state: WorkflowState) -> Color {
        switch state {
        case .completed: return .green
        case .failed: return .red
        case .unsupported: return .orange
        case .cancelled: return .secondary
        case .idle: return .secondary
        default: return .accentColor
        }
    }

    // MARK: - Report

    @ViewBuilder
    private func reportBlock(_ report: AnalysisReport) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 6) {
            GridRow {
                Text("Compatibility").foregroundStyle(.secondary)
                Text(report.supported ? "Supported by the Porte engine" : "Not supported yet")
            }
            GridRow {
                Text("Size").foregroundStyle(.secondary)
                Text(Formatters.bytes(report.size))
            }
            if let sha = report.sha256 {
                GridRow {
                    Text("SHA-256").foregroundStyle(.secondary)
                    Text(sha)
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                }
            }
        }
        .font(.callout)
        .gridColumnAlignment(.leading)

        ForEach(report.blockers, id: \.self) { blocker in
            Label(blocker, systemImage: "exclamationmark.triangle")
                .font(.callout)
                .foregroundStyle(.orange)
        }
        ForEach(report.notes, id: \.self) { note in
            Text(note)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        if let package = report.package {
            Text(packageSummary(package))
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        if report.canInstall {
            consentBlock
        }
    }

    private func packageSummary(_ package: PackageFacts) -> String {
        var parts: [String] = []
        if let version = package.version, !version.isEmpty { parts.append("Version \(version)") }
        if let publisher = package.publisher ?? package.manufacturer { parts.append(publisher) }
        if let files = package.files { parts.append("\(files) files") }
        if let registry = package.registryValues, registry > 0 {
            parts.append("\(registry) registry values")
        }
        if let shortcuts = package.shortcuts, shortcuts > 0 {
            parts.append("\(shortcuts) shortcuts")
        }
        return parts.joined(separator: " · ")
    }

    private var consentBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Porte will create a macOS application", systemImage: "lock.shield")
                .font(.callout.weight(.medium))
            Text("Windows programs are not native macOS software. Porte runs this "
                 + "program through its compatibility layer, and packaging it does not "
                 + "make an untrusted file safe. The app will be created in "
                 + "\(model.installationDestination.path).")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5),
                    in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    // MARK: - Results

    private func installBlock(_ result: InstallResult) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(result.bundles.isEmpty
                  ? "Package installed"
                  : "Installed and added to Applications",
                  systemImage: "checkmark.circle.fill")
                .font(.callout.weight(.medium))
                .foregroundStyle(.green)

            ForEach(result.bundles, id: \.app) { bundle in
                HStack(spacing: 10) {
                    AppIconView(path: bundle.app, size: 32)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(bundle.name ?? URL(fileURLWithPath: bundle.app).deletingPathExtension().lastPathComponent)
                            .font(.callout.weight(.medium))
                        Text(bundle.app)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    Spacer()
                    Button("Show in Finder") {
                        NSWorkspace.shared.activateFileViewerSelecting(
                            [URL(fileURLWithPath: bundle.app)])
                    }
                    .controlSize(.small)
                }
            }

            if result.bundles.isEmpty {
                Text("Porte installed the package contents, but no program in it "
                     + "can be launched by the engine yet.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !result.skippedExecutables.isEmpty {
                Text("Not launchable yet: "
                     + result.skippedExecutables.map(\.file).joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ForEach(result.warnings, id: \.self) { warning in
                Text(warning)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func runBlock(_ run: RunResult) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(run.output.isEmpty ? "The program produced no output." : run.output)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
                .background(Color(nsColor: .textBackgroundColor),
                            in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            Text("Exit status \(run.exitCode) · \(String(format: "%.1f", run.runMs)) ms"
                 + (run.fault.map { " · fault: \($0)" } ?? ""))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func errorBlock(_ message: String, workflow: Workflow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(message, systemImage: "exclamationmark.triangle.fill")
                .font(.callout)
                .foregroundStyle(.red)
                .fixedSize(horizontal: false, vertical: true)
            if workflow.canRetryWithForce {
                Text("An application with the same name is already installed. "
                     + "Replacing it removes the old bundle and environment.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Technical details

    private func technicalBlock(_ workflow: Workflow) -> some View {
        DisclosureGroup(isExpanded: $technical.value) {
            VStack(alignment: .leading, spacing: 6) {
                Text("File: \(workflow.fileURL.path)")
                if let report = workflow.report {
                    Text("Kind: \(report.kind) · profile: \(report.profile ?? "none")")
                    if !report.imports.isEmpty {
                        Text("Imports: \(report.imports.joined(separator: ", "))")
                    }
                    if !report.missingStubs.isEmpty {
                        Text("Missing host stubs: \(report.missingStubs.joined(separator: ", "))")
                    }
                }
                if let code = workflow.errorCode {
                    Text("Error code: \(code)")
                }
                ForEach(Array(workflow.log.enumerated()), id: \.offset) { _, line in
                    Text(line)
                }
            }
            .font(.caption.monospaced())
            .foregroundStyle(.secondary)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 6)
        } label: {
            Text("Technical Details")
                .font(.callout)
        }
    }

    // MARK: - Footer

    private func footer(_ workflow: Workflow) -> some View {
        HStack(spacing: 10) {
            if workflow.isBusy {
                Button("Cancel") { model.cancelWorkflow() }
                    .disabled(workflow.state == .installing)
            } else {
                Button("Close") { model.dismissWorkflow() }
                    .keyboardShortcut(.cancelAction)
                if workflow.report == nil || workflow.state.isFailure {
                    Button("Show in Finder") {
                        model.reveal(path: workflow.fileURL.path)
                    }
                }
                if workflow.report?.canRunNow == true,
                   workflow.runResult == nil,
                   workflow.installResult == nil {
                    Button("Run") { model.runCurrentWorkflow() }
                }
            }
            Spacer()
            if workflow.canRetryWithForce {
                Button("Replace Existing") {
                    model.installCurrentWorkflow(force: true)
                }
            } else if let installed = workflow.installResult?.bundles.first,
                      !workflow.isBusy {
                Button("Open") {
                    model.launchBundle(at: installed.app)
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
            } else if workflow.report?.canInstall == true,
                      workflow.installResult == nil,
                      !workflow.isBusy {
                Button("Install as App") {
                    model.installCurrentWorkflow()
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(16)
    }
}

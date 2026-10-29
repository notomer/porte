import PorteKit
import SwiftUI

/// The one-time setup. Two checks, one optional step, and a Continue button.
/// It never fabricates progress: every line is the result of a real probe.
struct SetupView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Form {
                runtimeSection
                associationSection
            }
            .formStyle(.grouped)
            Divider()
            footer
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 16) {
            Image(systemName: "macwindow.on.rectangle")
                .font(.system(size: 38, weight: .regular))
                .foregroundStyle(.tint)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text("Welcome to Porte")
                    .font(.largeTitle.weight(.semibold))
                Text("Your Windows apps. At home on Mac.")
                    .font(.title3)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 28)
        .padding(.top, 28)
        .padding(.bottom, 14)
    }

    private var healthy: Bool {
        model.hello?.health.ok == true
    }

    @ViewBuilder
    private var runtimeSection: some View {
        Section {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: model.canFinishSetup
                      ? (healthy ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                      : "xmark.circle.fill")
                    .font(.title2)
                    .foregroundStyle(model.canFinishSetup ? (healthy ? Color.green : Color.orange) : Color.red)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 3) {
                    Text(model.canFinishSetup ? "Compatibility runtime ready" : "Compatibility runtime required")
                        .font(.headline)
                    Text(model.runtimeDetail)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                Spacer(minLength: 8)
                if model.canFinishSetup {
                    Button("Check Again") { model.recheckRuntime() }
                } else {
                    Button("Choose Runtime…") { model.chooseRuntime() }
                        .buttonStyle(.borderedProminent)
                }
            }

            if let problems = model.hello?.health.problems, !problems.isEmpty {
                ForEach(problems, id: \.self) { problem in
                    Label(problem, systemImage: "exclamationmark.triangle")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }

            if let runtime = model.runtime, model.hello != nil {
                LabeledContent("Interpreter", value: runtime.python)
                if let version = model.hello?.version {
                    LabeledContent("Engine", value: "Porte \(version)")
                }
            }
        } header: {
            Text("Compatibility Runtime")
        } footer: {
            Text("Porte uses the compatibility runtime already installed on this Mac. "
                 + "Setup checks that the engine and its toolchain work; it does not "
                 + "download anything, and it needs no administrator privileges.")
                .font(.footnote)
        }
    }

    private var associationSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 10) {
                    Image(systemName: allDefault ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(allDefault ? Color.green : Color.secondary)
                        .accessibilityHidden(true)
                    Text(allDefault
                         ? "Porte is the default app for Windows files"
                         : "Porte is not the default app for Windows files")
                        .font(.body)
                    Spacer(minLength: 8)
                    Button(allDefault ? "Recheck" : "Make Porte the Default") {
                        model.makePorteDefault()
                    }
                    .disabled(!model.canFinishSetup)
                }
                Text(currentHandlers)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text("Optional. macOS may show its own confirmation before the change "
                     + "takes effect; you can also use Finder ▸ Get Info ▸ Open with.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } header: {
            Text("File Associations")
        }
    }

    private var footer: some View {
        HStack(alignment: .center, spacing: 12) {
            Text("Windows programs are not native macOS software.")
                .font(.footnote)
                .foregroundStyle(.secondary)
            Spacer()
            if !model.canFinishSetup {
                Text("Resolve the runtime check to continue.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Button("Continue") {
                model.completeSetup()
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .keyboardShortcut(.defaultAction)
            .disabled(!model.canFinishSetup)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
    }

    private var allDefault: Bool {
        !model.associations.isEmpty && model.associations.allSatisfy(\.isPorte)
    }

    private var currentHandlers: String {
        let parts = model.associations.map {
            "\($0.type.pathExtension.uppercased()) → \($0.summary)"
        }
        return parts.isEmpty ? "No handler information available." : parts.joined(separator: "   ·   ")
    }
}

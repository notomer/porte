import AppKit
import PorteKit
import SwiftUI

struct AppDetailView: View {
    let app: InstalledApplication
    let product: InstalledProduct?
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Form {
                Section {
                    LabeledContent("Bundle identifier", value: app.bundleId)
                    LabeledContent("Location", value: app.path)
                    LabeledContent("Runs", value: "\(app.runs)")
                    if let last = app.lastRun {
                        LabeledContent("Last run", value: last)
                    }
                } header: {
                    Text("Application")
                }

                if let product {
                    Section {
                        LabeledContent("Installed from", value: product.kindLabel)
                        if let version = product.version {
                            LabeledContent("Version", value: version)
                        }
                        if let manufacturer = product.manufacturer {
                            LabeledContent("Publisher", value: manufacturer)
                        }
                        LabeledContent("Files", value: "\(product.files ?? 0)")
                        if let environment = product.environment {
                            LabeledContent("Windows environment") {
                                Button("Show in Finder") {
                                    NSWorkspace.shared.activateFileViewerSelecting(
                                        [URL(fileURLWithPath: environment)])
                                }
                            }
                        }
                    } header: {
                        Text("Installed Package")
                    } footer: {
                        if !product.skippedExecutables.isEmpty {
                            VStack(alignment: .leading, spacing: 4) {
                                ForEach(product.skippedExecutables, id: \.file) { skipped in
                                    Text("\(skipped.file) cannot run yet: missing "
                                         + skipped.missing.prefix(4).joined(separator: ", "))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            }
            .formStyle(.grouped)

            Divider()
            footer
        }
        .frame(width: 580, height: 500)
        .task { await model.loadDiagnostics() }
    }

    private var header: some View {
        HStack(spacing: 14) {
            AppIconView(path: app.path, size: 56)
            VStack(alignment: .leading, spacing: 3) {
                Text(app.name)
                    .font(.title2.weight(.semibold))
                Text(app.profile == "metal-frame"
                     ? "Windows graphics application"
                     : "Windows application")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if !app.isReady {
                StatusBadge(text: app.statusLabel, tint: .orange)
            }
        }
        .padding(24)
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Button("Open") {
                model.launch(app)
                dismiss()
            }
            .buttonStyle(.borderedProminent)
            Button("Show in Finder") { model.reveal(app) }
            Spacer()
            Button("Uninstall…", role: .destructive) {
                model.uninstall(app)
                dismiss()
            }
            Button("Done") { dismiss() }
                .keyboardShortcut(.cancelAction)
        }
        .padding(16)
    }
}

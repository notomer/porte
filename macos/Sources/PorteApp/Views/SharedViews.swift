import AppKit
import PorteKit
import SwiftUI
import UniformTypeIdentifiers

/// Local view flags. A tiny observable object rather than `@State` so the app
/// builds with the Command Line Tools SDK, where the SwiftUI macro plugin is
/// absent (Xcode and CI provide it).
final class ViewFlag: ObservableObject {
    @Published var value: Bool
    init(_ value: Bool = false) { self.value = value }
}

struct LaunchView: View {
    var body: some View {
        VStack(spacing: 12) {
            ProgressView()
                .controlSize(.small)
            Text("Starting Porte…")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct AppIconView: View {
    let path: String
    var size: CGFloat = 64

    var body: some View {
        Image(nsImage: icon)
            .resizable()
            .interpolation(.high)
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }

    private var icon: NSImage {
        let image = NSWorkspace.shared.icon(forFile: path)
        image.size = NSSize(width: size, height: size)
        return image
    }
}

/// The icon the engine extracted from a Windows image, or a document icon when
/// the image has none. Never a placeholder that pretends to be the app's icon.
struct PEIconView: View {
    let base64: String?
    var size: CGFloat = 56

    var body: some View {
        if let base64, let data = Data(base64Encoded: base64), let image = NSImage(data: data) {
            Image(nsImage: image)
                .resizable()
                .interpolation(.high)
                .frame(width: size, height: size)
        } else {
            Image(nsImage: documentIcon)
                .resizable()
                .frame(width: size, height: size)
        }
    }

    private var documentIcon: NSImage {
        let type = UTType(filenameExtension: "exe") ?? .data
        let image = NSWorkspace.shared.icon(for: type)
        image.size = NSSize(width: size, height: size)
        return image
    }
}

struct StatusBadge: View {
    let text: String
    var tint: Color = .secondary

    var body: some View {
        Text(text)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(tint.opacity(0.14), in: Capsule())
            .foregroundStyle(tint)
    }
}

struct AppAlertModifier: ViewModifier {
    @Binding var alert: AppAlert?

    func body(content: Content) -> some View {
        content.alert(
            alert?.title ?? "",
            isPresented: Binding(get: { alert != nil },
                                 set: { if !$0 { alert = nil } }),
            presenting: alert
        ) { current in
            if let action = current.primaryAction {
                Button(current.primaryTitle,
                       role: current.destructive ? .destructive : nil) {
                    alert = nil
                    action()
                }
            } else {
                Button("OK", role: .cancel) { alert = nil }
            }
            if let secondary = current.secondaryTitle {
                Button(secondary, role: .cancel) { alert = nil }
            }
        } message: { current in
            Text(current.message)
        }
    }
}

extension View {
    func porteAlert(_ alert: Binding<AppAlert?>) -> some View {
        modifier(AppAlertModifier(alert: alert))
    }
}

enum Formatters {
    static func bytes(_ value: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(value), countStyle: .file)
    }
}

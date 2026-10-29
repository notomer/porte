import AppKit
import Foundation
import PorteKit

/// Small Launch Services tool used by `macos/verify.sh` and by anyone checking
/// which application macOS currently associates with .exe and .msi files.

private func usage() -> Never {
    FileHandle.standardError.write(Data("""
    usage:
      porte-ls status <exe|msi|uti> [app-path]
      porte-ls claim <app-path> <exe|msi|uti>
      porte-ls handlers [app-path]
      porte-ls find <bundle-id>

    """.utf8))
    exit(2)
}

private func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(2)
}

private func resolveType(_ raw: String) -> PorteFileType {
    let lowered = raw.lowercased()
    if let type = PorteFileType.allCases.first(where: {
        $0.pathExtension == lowered || $0.identifier == raw
    }) {
        return type
    }
    fail("unknown file type \(raw); use exe, msi or a UTI")
}

private func emit(_ document: [String: JSONValue]) {
    guard let data = try? JSONEncoder().encode(JSONValue.object(document)),
          let line = String(data: data, encoding: .utf8) else { return }
    print(line)
}

private func statusDocument(_ status: AssociationStatus) -> JSONValue {
    .object([
        "type": .string(status.type.rawValue),
        "uti": .string(status.type.identifier),
        "extension": .string(status.type.pathExtension),
        "handler": status.handlerPath.map { .string($0) } ?? .null,
        "handler_name": status.handlerName.map { .string($0) } ?? .null,
        "is_porte": .bool(status.isPorte),
        "porte_app": .string(status.portePath),
    ])
}

private func defaultPorteApp() -> URL {
    // When run from a build directory, look for the assembled app next to it.
    let candidate = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent("build/Porte.app")
    if FileManager.default.fileExists(atPath: candidate.path) {
        return candidate
    }
    return Bundle.main.bundleURL
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard let command = arguments.first else { usage() }

switch command {
case "status":
    guard arguments.count >= 2 else { usage() }
    let type = resolveType(arguments[1])
    let app = arguments.count >= 3
        ? URL(fileURLWithPath: arguments[2])
        : defaultPorteApp()
    emit(["status": statusDocument(FileAssociations.status(for: type, porteApp: app))])

case "claim":
    guard arguments.count >= 3 else { usage() }
    let app = URL(fileURLWithPath: arguments[1])
    guard FileManager.default.fileExists(atPath: app.path) else {
        fail("no application at \(app.path)")
    }
    let type = resolveType(arguments[2])
    do {
        try FileAssociations.setDefaultApplication(app, for: type)
    } catch {
        fail("macOS refused to set \(app.lastPathComponent) for .\(type.pathExtension): "
             + error.localizedDescription)
    }
    emit(["claimed": statusDocument(FileAssociations.status(for: type, porteApp: app))])

case "handlers":
    let app = arguments.count >= 2
        ? URL(fileURLWithPath: arguments[1])
        : defaultPorteApp()
    emit(["handlers": .array(FileAssociations.allStatuses(porteApp: app).map(statusDocument))])

case "find":
    guard arguments.count >= 2 else { usage() }
    if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: arguments[1]) {
        print(url.path)
    } else {
        exit(1)
    }

default:
    usage()
}

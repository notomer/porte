import AppKit
import Foundation
import PorteKit
import UniformTypeIdentifiers

/// Deterministic checks for the non-UI layer of the macOS app.
///
/// This is a plain executable rather than an XCTest bundle so it runs with the
/// Command Line Tools alone (`swift run porte-check`). CI runs the same binary.

private struct CheckFailure: Error, CustomStringConvertible {
    let description: String
}

private func expect(_ condition: Bool, _ message: @autoclosure () -> String) throws {
    if !condition { throw CheckFailure(description: message()) }
}

private func expectEqual<T: Equatable>(_ lhs: T, _ rhs: T, _ label: String) throws {
    if lhs != rhs { throw CheckFailure(description: "\(label): \(lhs) != \(rhs)") }
}

private struct TemporaryDirectory {
    let url: URL
    init() {
        url = FileManager.default.temporaryDirectory
            .appendingPathComponent("porte-check-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    }
    func cleanUp() {
        try? FileManager.default.removeItem(at: url)
    }
}

private final class Clock: @unchecked Sendable {
    var now: Date
    init(_ now: Date) { self.now = now }
}

private let checks: [(String, () throws -> Void)] = [
    ("json value round trip", {
        let value = JSONValue.object([
            "text": .string("hello"),
            "count": .number(3),
            "flag": .bool(true),
            "list": .array([.number(1), .null]),
        ])
        let data = try JSONEncoder().encode(value)
        let decoded = try JSONDecoder().decode(JSONValue.self, from: data)
        try expectEqual(decoded, value, "round trip")
        try expectEqual(decoded["count"]?.intValue, 3, "int accessor")
        try expectEqual(decoded["list"]?.arrayValue?.count, 2, "array accessor")
        try expect(decoded["missing"] == nil, "missing key")
    }),

    ("bridge event decoding", {
        let ready = try BridgeEvent.decode(
            line: #"{"event":"ready","protocol":1,"version":"0.1.0","python":"/usr/bin/python3","root":"/tmp/porte","pid":42}"#)
        try expect(ready.isReady, "ready event")
        try expectEqual(ready.raw["pid"]?.intValue, 42, "pid")
        try expectEqual(ready.raw["protocol"]?.intValue, 1, "protocol")

        let state = try BridgeEvent.decode(
            line: #"{"id":"r1","event":"state","state":"installing","message":"Installing…"}"#)
        try expectEqual(state.id, "r1", "id")
        try expectEqual(state.state, "installing", "state")

        let failure = try BridgeEvent.decode(
            line: #"{"id":"r2","event":"error","code":"unsupported","message":"nope"}"#)
        try expect(failure.isError, "error event")
        try expectEqual(failure.code, "unsupported", "code")

        let numericId = try BridgeEvent.decode(
            line: #"{"id":7,"event":"result","data":{"ok":true}}"#)
        try expectEqual(numericId.id, "7", "numeric id")

        do {
            _ = try BridgeEvent.decode(line: "definitely not json")
            throw CheckFailure(description: "malformed line was accepted")
        } catch is BridgeError {
            // expected
        }
    }),

    ("file open queue deduplicates and preserves order", {
        let clock = Clock(Date(timeIntervalSince1970: 1_000))
        let queue = FileOpenQueue(dedupeWindow: 2) { clock.now }
        let a = URL(fileURLWithPath: "/tmp/a.exe")
        let b = URL(fileURLWithPath: "/tmp/b.msi")
        try expectEqual(queue.accept([a, b]), [a, b], "first delivery")
        try expectEqual(queue.accept([a]), [], "duplicate suppressed")
        clock.now = clock.now.addingTimeInterval(3)
        try expectEqual(queue.accept([a]), [a], "re-open after the window")
        try expectEqual(queue.accept([URL(string: "https://example.com/x.exe")!]), [],
                        "non-file URLs are ignored")
    }),

    ("workflow states map from the bridge", {
        try expectEqual(WorkflowState(bridgeState: "analyzing"), .analyzing, "analyzing")
        try expectEqual(WorkflowState(bridgeState: "bundling"), .installing, "bundling")
        try expectEqual(WorkflowState(bridgeState: "unsupported"), .unsupported, "unsupported")
        try expectEqual(WorkflowState(bridgeState: "nonsense"), .idle, "unknown")
        try expect(WorkflowState.installing.isBusy, "installing is busy")
        try expect(!WorkflowState.completed.isBusy, "completed is not busy")
        try expect(WorkflowState.failed.isFailure, "failed is a failure")
    }),

    ("analysis report decodes typed fields", {
        let json = #"""
        {"path":"/tmp/Demo.exe","name":"Demo","suffix":".exe","size":1024,
         "sha256":"aaaa","kind":"exe-portable","supported":true,
         "install_supported":true,"run_supported":true,"profile":"console",
         "blockers":[],"warnings":[],"imports":["GetCommandLineA"],
         "missing_stubs":[],"notes":[],"icon_png":null,"installed":null,
         "package":null,"actions":["install","run"],
         "summary":"Portable Windows application."}
        """#
        let report = try JSONValue.decodeJSON(Data(json.utf8)).decoded(AnalysisReport.self)
        try expectEqual(report.kind, "exe-portable", "kind")
        try expect(report.canInstall && report.canRunNow, "actions")
        try expectEqual(report.size, 1024, "size")
        try expectEqual(report.imports, ["GetCommandLineA"], "imports")
    }),

    ("library snapshot decodes and computes readiness", {
        let json = #"""
        {"applications":[{"id":"com.notomer.porte.windows.demo-1","bundle_id":"com.notomer.porte.windows.demo-1",
          "name":"Demo","path":"/tmp/Demo.app","profile":"console","runs":2,
          "status":"runtime-missing","problem":"moved","state_dir":"/tmp/state",
          "last_run":"2026-10-29T00:00:00Z","product_key":null,"arguments":[]}],
         "products":[{"product_key":"demo-1","name":"Demo","version":"1.0.0","kind":"msi",
          "manufacturer":"Example","installed_at":"2026-10-29T00:00:00Z",
          "environment":"/tmp/env","files":3,"registry_values":1,
          "bundles":[{"app":"/tmp/Demo.app","bundle_id":"x","exe":"Demo.exe"}],
          "skipped_executables":[{"file":"Gui.exe","missing":["user32.dll"]}],
          "warnings":[],"package":"/tmp/Demo.msi"}]}
        """#
        let snapshot = try JSONValue.decodeJSON(Data(json.utf8)).decoded(LibrarySnapshot.self)
        try expectEqual(snapshot.applications.count, 1, "applications")
        try expectEqual(snapshot.products.count, 1, "products")
        try expect(!snapshot.applications[0].isReady, "status")
        try expectEqual(snapshot.applications[0].statusLabel, "Runtime missing", "label")
        try expectEqual(snapshot.products[0].bundles.first?.app, "/tmp/Demo.app", "bundle ref")
        try expectEqual(snapshot.products[0].skippedExecutables.first?.missing,
                        ["user32.dll"], "skipped")
    }),

    ("diagnostics report decodes", {
        let json = #"""
        {"protocol":1,"version":"0.1.0","python":"/usr/bin/python3","root":"/tmp/porte",
         "support_root":"/tmp/support","applications_root":"/tmp/apps","products":[],
         "cache_bytes":512,"recent_runs":[],
         "health":{"engine":true,"modules":{},"clang":true,"iconutil":true,
                   "writable":true,"launcher_source":true,"problems":[],"ok":true,
                   "support_root":"/tmp/support"},
         "host":{"os":"27.2"},"toolchain":{"clang":"clang 21","clang_path":"/usr/bin/clang","sdk":"27"}}
        """#
        let report = try JSONValue.decodeJSON(Data(json.utf8)).decoded(DiagnosticsReport.self)
        try expectEqual(report.protocolVersion, 1, "protocol")
        try expect(report.health.ok, "health")
        try expectEqual(report.cacheBytes, 512, "cache")
    }),

    ("runtime descriptor validates its files", {
        let directory = TemporaryDirectory()
        defer { directory.cleanUp() }
        let python = directory.url.appendingPathComponent("python3")
        try "#!/bin/sh\n".write(to: python, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755],
                                              ofItemAtPath: python.path)
        let package = directory.url.appendingPathComponent("porte/gui", isDirectory: true)
        try FileManager.default.createDirectory(at: package, withIntermediateDirectories: true)
        try "".write(to: package.appendingPathComponent("bridge.py"), atomically: true,
                     encoding: .utf8)
        let descriptor = RuntimeDescriptor(python: python.path, root: directory.url.path)
        try expect(descriptor.isValid, "valid descriptor")
        try expect(descriptor.healthProblem == nil, "no health problem")
        let broken = RuntimeDescriptor(python: python.path,
                                       root: directory.url.appendingPathComponent("missing").path)
        try expect(!broken.isValid, "missing root rejected")
        try expect(broken.healthProblem != nil, "problem described")
    }),

    ("setup store persists and validates", {
        let directory = TemporaryDirectory()
        defer { directory.cleanUp() }
        let store = SetupStore(baseDirectory: directory.url)
        let runtime = RuntimeDescriptor(python: "/usr/bin/python3", root: "/tmp/porte")
        try store.save(runtime: runtime)
        try expectEqual(store.loadSavedRuntime(), runtime, "saved runtime")
        var state = SetupState()
        try expect(state.needsSetup(validRuntime: true), "empty state needs setup")
        state.completedAt = Date()
        state.runtime = runtime
        state.fileAssociations = "set"
        try store.save(state)
        let loaded = store.load()
        try expect(loaded.isComplete, "completed")
        try expect(loaded.completedAt != nil, "timestamp")
        try expectEqual(loaded.fileAssociations, "set", "associations")
        try expect(!loaded.needsSetup(validRuntime: true), "no setup needed")
        try expect(loaded.needsSetup(validRuntime: false), "setup needed when runtime broke")
        try store.clear()
        try expect(!store.load().isComplete, "cleared")
    }),

    ("file types and Info.plist agree", {
        try expectEqual(PorteFileType.windowsExecutable.identifier,
                        "com.microsoft.windows-executable", "exe UTI")
        try expectEqual(PorteFileType.windowsInstaller.identifier,
                        "com.notomer.porte.windows-msi", "msi UTI")
        try expectEqual(UTType(filenameExtension: "exe")?.identifier,
                        "com.microsoft.windows-executable", "system exe type")
        try expect(FileAssociations.isWindowsProgram(URL(fileURLWithPath: "/tmp/a.exe")), "exe")
        try expect(FileAssociations.isWindowsProgram(URL(fileURLWithPath: "/tmp/a.msi")), "msi")
        try expect(!FileAssociations.isWindowsProgram(URL(fileURLWithPath: "/tmp/a.txt")), "txt")

        let macos = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // PorteCheck
            .deletingLastPathComponent()   // Sources
            .deletingLastPathComponent()   // macos
        let plist = macos.appendingPathComponent("Resources/Info.plist")
        let data = try Data(contentsOf: plist)
        let document = try PropertyListSerialization.propertyList(from: data, format: nil)
        guard let info = document as? [String: Any] else {
            throw CheckFailure(description: "Info.plist is not a dictionary")
        }
        try expectEqual(info["CFBundleIdentifier"] as? String, "com.notomer.porte", "bundle id")
        try expectEqual(info["CFBundleExecutable"] as? String, "Porte", "executable")
        guard let types = info["CFBundleDocumentTypes"] as? [[String: Any]] else {
            throw CheckFailure(description: "no CFBundleDocumentTypes")
        }
        let contentTypes = types.flatMap { $0["LSItemContentTypes"] as? [String] ?? [] }
        try expect(contentTypes.contains(PorteFileType.windowsExecutable.identifier),
                   "exe document type")
        try expect(contentTypes.contains(PorteFileType.windowsInstaller.identifier),
                   "msi document type")
        for type in types {
            try expectEqual(type["LSHandlerRank"] as? String, "Alternate", "handler rank")
        }
        guard let imported = info["UTImportedTypeDeclarations"] as? [[String: Any]],
              let msi = imported.first(where: {
                  ($0["UTTypeIdentifier"] as? String) == PorteFileType.windowsInstaller.identifier
              }) else {
            throw CheckFailure(description: "msi type is not imported")
        }
        let tags = msi["UTTypeTagSpecification"] as? [String: Any]
        let extensions = tags?["public.filename-extension"] as? [String]
        try expectEqual(extensions, ["msi"], "msi extension tag")
        try expect(info["LSUIElement"] == nil, "Porte is a regular application")
    }),

    ("runtime discovery finds the development checkout", {
        let macos = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let expected = macos.deletingLastPathComponent()
        if FileManager.default.isExecutableFile(
            atPath: expected.appendingPathComponent(".venv/bin/python").path) {
            let found = RuntimeLocator.developmentDescriptor(startingAt: macos)
            try expect(found != nil, "development runtime not found")
            try expectEqual(found?.root ?? "", expected.path, "repository root")
            try expect(found?.isValid == true, "development runtime invalid")
        }
    }),
]

/// Starts the real bridge with the development runtime and speaks protocol 1.
/// Skipped (not silently passed) when there is no source checkout runtime.
private func bridgeCheck() async -> (name: String, error: String?)? {
    guard let runtime = RuntimeLocator.developmentDescriptor(), runtime.isValid else {
        return nil
    }
    let bridge = PorteBridge(runtime: runtime)
    do {
        let ready = try await bridge.start()
        guard ready.protocolVersion == 1 else {
            await bridge.shutdown()
            return ("engine bridge handshake and library",
                    "unexpected protocol \(ready.protocolVersion)")
        }
        let document = try await bridge.request("hello")
        let hello = try document.decoded(HelloInfo.self)
        guard hello.protocolVersion == 1 else {
            await bridge.shutdown()
            return ("engine bridge handshake and library", "hello protocol mismatch")
        }
        guard hello.health.engine else {
            await bridge.shutdown()
            return ("engine bridge handshake and library", "engine reported unhealthy")
        }
        let library = try await bridge.request(
            "library", args: ["dest": .string(FileManager.default.temporaryDirectory.path)])
        _ = try library.decoded(LibrarySnapshot.self)
        await bridge.shutdown()
        return ("engine bridge handshake and library", nil)
    } catch {
        let details = await bridge.diagnostics()
        await bridge.shutdown()
        return ("engine bridge handshake and library", "\(error)\n\(details)")
    }
}

@main
struct PorteCheckMain {
    static func main() async {
        var failures = 0
        var ran = 0
        for (name, check) in checks {
            ran += 1
            do {
                try check()
                print("ok    \(name)")
            } catch {
                failures += 1
                print("FAIL  \(name): \(error)")
            }
        }
        if let result = await bridgeCheck() {
            ran += 1
            if let error = result.error {
                failures += 1
                print("FAIL  \(result.name): \(error)")
            } else {
                print("ok    \(result.name)")
            }
        } else {
            print("skip  engine bridge handshake and library (no development runtime)")
        }
        print("")
        if failures == 0 {
            print("\(ran) checks passed")
            exit(0)
        }
        print("\(failures) of \(ran) checks failed")
        exit(1)
    }
}

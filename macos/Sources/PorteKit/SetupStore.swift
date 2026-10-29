import Foundation

/// Persisted result of the one-time setup.
///
/// Setup is re-run only when the recorded runtime is missing or broken, so the
/// state records exactly which runtime was validated and whether the optional
/// file-association step was offered.
public struct SetupState: Codable, Equatable, Sendable {
    public var schema: Int
    public var completedAt: Date?
    public var runtime: RuntimeDescriptor?
    /// "unset", "set", "declined" or "unavailable"
    public var fileAssociations: String
    /// Where the user said their compatibility runtime is, when it is not in a
    /// location Porte discovers on its own. Passed to the engine as
    /// `PORTE_WINE_ROOT`; it is a pointer, never a copy or a download.
    public var compatibilityRuntimePath: String?

    public init(schema: Int = 1, completedAt: Date? = nil,
                runtime: RuntimeDescriptor? = nil,
                fileAssociations: String = "unset",
                compatibilityRuntimePath: String? = nil) {
        self.schema = schema
        self.completedAt = completedAt
        self.runtime = runtime
        self.fileAssociations = fileAssociations
        self.compatibilityRuntimePath = compatibilityRuntimePath
    }

    public var isComplete: Bool { completedAt != nil && runtime != nil }

    public func needsSetup(validRuntime: Bool) -> Bool {
        !isComplete || !validRuntime
    }
}

public final class SetupStore: @unchecked Sendable {
    public let baseDirectory: URL
    private let lock = NSLock()

    public init(baseDirectory: URL = SetupStore.defaultBaseDirectory) {
        self.baseDirectory = baseDirectory
    }

    public static var defaultBaseDirectory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("Porte/GUI", isDirectory: true)
    }

    public var stateURL: URL { baseDirectory.appendingPathComponent("setup.json") }
    public var eventsURL: URL { baseDirectory.appendingPathComponent("events.jsonl") }
    public var savedRuntimeURL: URL { baseDirectory.appendingPathComponent("runtime.json") }

    public func load() -> SetupState {
        lock.lock()
        defer { lock.unlock() }
        guard let data = try? Data(contentsOf: stateURL) else {
            return SetupState()
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        do {
            return try decoder.decode(SetupState.self, from: data)
        } catch {
            return SetupState()
        }
    }

    public func save(_ state: SetupState) throws {
        lock.lock()
        defer { lock.unlock() }
        try FileManager.default.createDirectory(at: baseDirectory,
                                                withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(state).write(to: stateURL, options: .atomic)
    }

    public func save(runtime: RuntimeDescriptor) throws {
        lock.lock()
        defer { lock.unlock() }
        try FileManager.default.createDirectory(at: baseDirectory,
                                                withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(runtime).write(to: savedRuntimeURL, options: .atomic)
    }

    public func loadSavedRuntime() -> RuntimeDescriptor? {
        lock.lock()
        defer { lock.unlock() }
        guard let data = try? Data(contentsOf: savedRuntimeURL) else { return nil }
        return try? JSONDecoder().decode(RuntimeDescriptor.self, from: data)
    }

    public func clear() throws {
        lock.lock()
        defer { lock.unlock() }
        let manager = FileManager.default
        for url in [stateURL, savedRuntimeURL] where manager.fileExists(atPath: url.path) {
            try manager.removeItem(at: url)
        }
    }
}

/// Append-only diagnostics log used by the app and by `macos/verify.sh`.
///
/// It records what actually reached the app (a Finder open request, an analysis
/// result, an install), never interaction it wishes had happened.
public final class EventLog: @unchecked Sendable {
    public let url: URL
    private let lock = NSLock()

    public init(url: URL) {
        self.url = url
    }

    public func record(_ event: String, _ fields: [String: JSONValue] = [:]) {
        lock.lock()
        defer { lock.unlock() }
        var document = fields
        document["event"] = .string(event)
        document["at"] = .string(ISO8601DateFormatter().string(from: Date()))
        document["pid"] = .number(Double(ProcessInfo.processInfo.processIdentifier))
        guard let data = try? JSONEncoder().encode(JSONValue.object(document)),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        let manager = FileManager.default
        try? manager.createDirectory(at: url.deletingLastPathComponent(),
                                     withIntermediateDirectories: true)
        if !manager.fileExists(atPath: url.path) {
            manager.createFile(atPath: url.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: url) else { return }
        defer { try? handle.close() }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: Data(line.utf8))
    }
}

import Foundation

public struct RuntimeSelectionError: LocalizedError, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

/// Finds the Porte engine the app should talk to.
///
/// Order, most specific first:
/// 1. `PORTE_RUNTIME_PYTHON` / `PORTE_RUNTIME_ROOT` (development and tests)
/// 2. a runtime the user chose in setup (`runtime.json`)
/// 3. the runtime recorded inside the app bundle by `macos/build.sh`
/// 4. a repository checkout containing `.venv` (running from source)
///
/// Each candidate is validated against the files it claims, and a stale saved
/// runtime is skipped rather than trusted.
public enum RuntimeLocator {
    public static func bundledDescriptor(bundle: Bundle = .main) -> RuntimeDescriptor? {
        guard let url = bundle.url(forResource: "porte-runtime", withExtension: "json"),
              let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(RuntimeDescriptor.self, from: data)
    }

    public static func environmentDescriptor(
        _ environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> RuntimeDescriptor? {
        guard let python = environment["PORTE_RUNTIME_PYTHON"], !python.isEmpty else {
            return nil
        }
        let root = environment["PORTE_RUNTIME_ROOT"]
            ?? environment["PORTE_ROOT"]
            ?? fallbackRoot(forPython: URL(fileURLWithPath: python))?.path
            ?? ""
        guard !root.isEmpty else { return nil }
        let pythonPath = (environment["PORTE_RUNTIME_PYTHONPATH"] ?? "")
            .split(separator: ":")
            .map(String.init)
        return RuntimeDescriptor(python: python, root: root,
                                 pythonPath: pythonPath.isEmpty ? nil : pythonPath)
    }

    /// The repository this executable belongs to, if it is running from source.
    public static func developmentDescriptor(
        startingAt start: URL? = nil,
        fileManager: FileManager = .default
    ) -> RuntimeDescriptor? {
        var roots: [URL] = []
        if let start {
            roots.append(start)
        } else {
            roots.append(URL(fileURLWithPath: fileManager.currentDirectoryPath))
            roots.append(Bundle.main.bundleURL)
            roots.append(Bundle.main.executableURL?.deletingLastPathComponent()
                         ?? URL(fileURLWithPath: fileManager.currentDirectoryPath))
        }
        var seen: Set<String> = []
        for root in roots {
            var current = root.standardizedFileURL
            for _ in 0..<7 {
                let key = current.path
                if !seen.contains(key) {
                    seen.insert(key)
                    let python = current.appendingPathComponent(".venv/bin/python")
                    let bridge = current.appendingPathComponent("porte/gui/bridge.py")
                    if fileManager.isExecutableFile(atPath: python.path),
                       fileManager.fileExists(atPath: bridge.path) {
                        return RuntimeDescriptor(python: python.path, root: current.path)
                    }
                }
                let parent = current.deletingLastPathComponent()
                if parent.path == current.path { break }
                current = parent
            }
        }
        return nil
    }

    public static func discover(
        bundle: Bundle = .main,
        store: SetupStore = SetupStore(),
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> RuntimeDescriptor? {
        var candidates: [RuntimeDescriptor] = []
        if let env = environmentDescriptor(environment) { candidates.append(env) }
        if let saved = store.loadSavedRuntime() { candidates.append(saved) }
        if let bundled = bundledDescriptor(bundle: bundle) { candidates.append(bundled) }
        if !isPackaged(bundle) {
            // Only an unbundled `swift run` build may resolve a runtime from the
            // current directory. A packaged app must not inherit the runtime of
            // whatever folder it happened to be launched from.
            if let dev = developmentDescriptor() { candidates.append(dev) }
        }
        return candidates.first(where: \.isValid)
    }

    public static func isPackaged(_ bundle: Bundle = .main) -> Bool {
        let url = bundle.bundleURL
        return url.pathExtension == "app"
            && FileManager.default.fileExists(
                atPath: url.appendingPathComponent("Contents/Info.plist").path)
    }

    /// Ask an interpreter to describe the Porte package it can import.
    public static func probe(python: URL, timeout: TimeInterval = 25) throws -> RuntimeDescriptor {
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw RuntimeSelectionError("\(python.path) is not an executable file.")
        }
        let script = """
        import json, os, sys
        try:
            import porte
        except Exception as exc:
            print(json.dumps({"error": "cannot import porte: %s" % exc}))
            sys.exit(3)
        root = os.path.dirname(os.path.dirname(os.path.abspath(porte.__file__)))
        try:
            from importlib.metadata import version as pkg_version
            v = pkg_version("porte")
        except Exception:
            v = None
        try:
            from porte.app.bundle import runtime_interpreter
            exe, pythonpath = runtime_interpreter()
        except Exception:
            exe = sys.executable
            pythonpath = [p for p in sys.path
                          if "site-packages" in p or "dist-packages" in p]
        print(json.dumps({"python": exe, "root": root,
                          "pythonpath": pythonpath, "version": v}))
        """
        let process = Process()
        process.executableURL = python
        process.arguments = ["-c", script]
        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        do {
            try process.run()
        } catch {
            throw RuntimeSelectionError("Could not run \(python.path): \(error.localizedDescription)")
        }
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.03)
        }
        if process.isRunning {
            process.terminate()
            throw RuntimeSelectionError("\(python.lastPathComponent) did not respond in time.")
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errorData = err.fileHandleForReading.readDataToEndOfFile()
        let stderr = String(data: errorData, encoding: .utf8) ?? ""
        guard let text = String(data: data, encoding: .utf8),
              let line = text.split(separator: "\n").last(where: { $0.hasPrefix("{") }),
              let document = try? JSONDecoder().decode(JSONValue.self, from: Data(line.utf8))
        else {
            throw RuntimeSelectionError(
                "This interpreter did not describe a Porte installation."
                + (stderr.isEmpty ? "" : "\n\(stderr.suffix(400))"))
        }
        if let message = document["error"]?.stringValue {
            throw RuntimeSelectionError(message)
        }
        guard let pythonPath = document["python"]?.stringValue,
              let root = document["root"]?.stringValue else {
            throw RuntimeSelectionError("The interpreter returned an incomplete runtime description.")
        }
        let paths = document["pythonpath"].flatMap { $0.arrayValue?.compactMap(\.stringValue) } ?? [root]
        let descriptor = RuntimeDescriptor(
            python: pythonPath,
            root: root,
            pythonPath: paths,
            version: document["version"]?.stringValue)
        if let problem = descriptor.healthProblem {
            throw RuntimeSelectionError(problem)
        }
        return descriptor
    }

    /// Interpret what the user chose in the setup file panel.
    public static func descriptor(forSelection selection: URL) throws -> RuntimeDescriptor {
        var candidate = selection.standardizedFileURL
        let values = try? candidate.resourceValues(forKeys: [.isDirectoryKey])
        if values?.isDirectory == true {
            let venv = candidate.appendingPathComponent(".venv/bin/python")
            if FileManager.default.isExecutableFile(atPath: venv.path) {
                candidate = venv
            } else {
                throw RuntimeSelectionError(
                    "\(candidate.lastPathComponent) does not contain .venv/bin/python. "
                    + "Choose the Python interpreter from a Porte environment, or the "
                    + "`porte` command itself.")
            }
        }
        if candidate.lastPathComponent == "porte",
           let first = firstLine(at: candidate),
           first.hasPrefix("#!") {
            let interpreter = first.dropFirst(2).trimmingCharacters(in: .whitespaces)
            if interpreter.hasPrefix("/") {
                candidate = URL(fileURLWithPath: interpreter)
            }
        }
        return try probe(python: candidate)
    }

    private static func firstLine(at url: URL) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        let data = (try? handle.read(upToCount: 512)) ?? Data()
        return String(data: data, encoding: .utf8)?
            .split(separator: "\n", maxSplits: 1).first.map(String.init)
    }

    private static func fallbackRoot(forPython python: URL) -> URL? {
        // .venv/bin/python → .venv's parent is the repository.
        let bin = python.deletingLastPathComponent()
        let venv = bin.deletingLastPathComponent()
        if venv.lastPathComponent == ".venv" || venv.lastPathComponent == "venv" {
            return venv.deletingLastPathComponent()
        }
        return nil
    }
}

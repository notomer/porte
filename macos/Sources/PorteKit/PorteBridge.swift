import Foundation

/// Client for the Porte engine's structured bridge (`porte.gui.bridge`).
///
/// The engine runs as a child process and speaks newline-delimited JSON. This
/// actor owns that process, correlates responses by request id, and forwards
/// progress events. Nothing here parses CLI text: the CLI and the GUI call the
/// same engine modules through different front ends.
public actor PorteBridge {
    public struct Ready: Sendable, Equatable {
        public let protocolVersion: Int
        public let version: String
        public let python: String
        public let root: String
        public let pid: Int32
    }

    private struct Pending {
        let continuation: CheckedContinuation<JSONValue, Error>
        let onEvent: (@Sendable (BridgeEvent) -> Void)?
    }

    private let runtime: RuntimeDescriptor
    /// Extra environment for the engine process, such as the user's choice of
    /// compatibility runtime (`PORTE_WINE_ROOT`).
    private let environmentOverrides: [String: String]
    private var process: Process?
    private var input: FileHandle?
    private var stdoutHandle: FileHandle?
    private var stderrHandle: FileHandle?
    private var stdoutBuffer = LineBuffer()
    private var stderrBuffer = LineBuffer()
    private var pending: [String: Pending] = [:]
    private var readyWaiters: [CheckedContinuation<Ready, Error>] = []
    private var nextRequest = 0
    private var ready: Ready?
    private var stderrTail: [String] = []
    private var isShuttingDown = false

    public init(runtime: RuntimeDescriptor,
                environment: [String: String] = [:]) {
        self.runtime = runtime
        self.environmentOverrides = environment
    }

    public var descriptor: RuntimeDescriptor { runtime }

    // MARK: - Lifecycle

    /// Starts the bridge and waits for its `ready` line. When the engine cannot
    /// start at all, this fails after `handshakeTimeout` instead of hanging.
    @discardableResult
    public func start(handshakeTimeout: TimeInterval = 30) async throws -> Ready {
        if let ready, process?.isRunning == true {
            return ready
        }
        if process == nil || process?.isRunning != true {
            process = nil
            input = nil
            ready = nil
            try launch()
        }
        if let ready { return ready }
        return try await withCheckedThrowingContinuation { continuation in
            readyWaiters.append(continuation)
            Task.detached(priority: .utility) { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(handshakeTimeout * 1_000_000_000))
                await self?.handshakeExpired()
            }
        }
    }

    /// Fails every waiting start exactly once and stops a runtime that never
    /// became ready. The bridge is restarted on the next request.
    private func handshakeExpired() {
        guard ready == nil, !readyWaiters.isEmpty else { return }
        let detail = stderrTail.suffix(10).joined(separator: "\n")
        let waiters = readyWaiters
        readyWaiters.removeAll()
        for waiter in waiters {
            waiter.resume(throwing: BridgeError.handshakeTimedOut(stderr: detail))
        }
        if let process, process.isRunning {
            process.terminate()
        }
    }

    private func launch() throws {
        let process = Process()
        process.executableURL = runtime.pythonURL
        process.arguments = ["-u", "-m", "porte.gui.bridge"]
        process.currentDirectoryURL = runtime.rootURL
        var environment = ProcessInfo.processInfo.environment
        let path = runtime.pythonPath.isEmpty ? [runtime.root] : runtime.pythonPath
        environment["PYTHONPATH"] = path.joined(separator: ":")
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PORTE_GUI"] = "1"
        for (key, value) in environmentOverrides where !value.isEmpty {
            environment[key] = value
        }
        process.environment = environment

        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardInput = stdinPipe
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        process.terminationHandler = { [weak self] finished in
            Task { await self?.exited(status: finished.terminationStatus) }
        }
        try process.run()
        self.process = process
        self.input = stdinPipe.fileHandleForWriting

        let stdout = stdoutPipe.fileHandleForReading
        let stderr = stderrPipe.fileHandleForReading
        stdoutHandle = stdout
        stderrHandle = stderr
        stdoutBuffer = LineBuffer()
        stderrBuffer = LineBuffer()
        stderrTail.removeAll()
        readLines(from: stdout, into: stdoutBuffer, isError: false)
        readLines(from: stderr, into: stderrBuffer, isError: true)
    }

    /// Reads with a readability handler rather than `AsyncBytes`. The handler is
    /// the documented, long-standing way to stream a child's pipe and does not
    /// depend on the async sequence machinery being scheduled in a CLI process.
    private nonisolated func readLines(from handle: FileHandle,
                                       into buffer: LineBuffer,
                                       isError: Bool) {
        handle.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            let lines = buffer.append(data)
            guard !lines.isEmpty else { return }
            Task { [weak self] in
                for line in lines {
                    await self?.ingest(line, isError: isError)
                }
            }
        }
    }

    private func ingest(_ line: String, isError: Bool) {
        if isError {
            appendDiagnostic(line)
            return
        }
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        do {
            deliver(try BridgeEvent.decode(line: trimmed))
        } catch {
            appendDiagnostic("protocol violation: \(trimmed.prefix(200))")
        }
    }

    private func appendDiagnostic(_ line: String) {
        guard !line.isEmpty else { return }
        stderrTail.append(line)
        if stderrTail.count > 300 {
            stderrTail.removeFirst(stderrTail.count - 300)
        }
    }

    private func deliver(_ event: BridgeEvent) {
        if event.isReady {
            let ready = Ready(
                protocolVersion: event.raw["protocol"]?.intValue ?? 0,
                version: event.raw["version"]?.stringValue ?? "",
                python: event.raw["python"]?.stringValue ?? runtime.python,
                root: event.raw["root"]?.stringValue ?? runtime.root,
                pid: Int32(event.raw["pid"]?.intValue ?? 0))
            self.ready = ready
            let waiters = readyWaiters
            readyWaiters.removeAll()
            for waiter in waiters {
                waiter.resume(returning: ready)
            }
            return
        }

        guard let id = event.id, let waiter = pending[id] else {
            // The only events without a live request are replies to `cancel`,
            // which the UI sends without waiting.
            return
        }
        if event.isError {
            pending.removeValue(forKey: id)
            waiter.continuation.resume(throwing: BridgeFailure(
                code: event.code ?? "error",
                message: event.message ?? "The operation failed.",
                data: event.data))
            return
        }
        if event.isResult {
            pending.removeValue(forKey: id)
            waiter.continuation.resume(returning: event.data ?? .null)
            return
        }
        waiter.onEvent?(event)
    }

    private func exited(status: Int32) {
        guard !isShuttingDown else { return }
        let detail = stderrTail.suffix(8).joined(separator: "\n")
        reset()
        failAll(BridgeError.processExited(status: status, detail: detail))
    }

    private func reset() {
        stdoutHandle?.readabilityHandler = nil
        stderrHandle?.readabilityHandler = nil
        stdoutHandle = nil
        stderrHandle = nil
        process = nil
        input = nil
        ready = nil
    }

    private func failAll(_ error: Error) {
        let waiting = pending
        pending.removeAll()
        for (_, waiter) in waiting {
            waiter.continuation.resume(throwing: error)
        }
        let readyWaiting = readyWaiters
        readyWaiters.removeAll()
        for waiter in readyWaiting {
            waiter.resume(throwing: error)
        }
    }

    public func shutdown() {
        isShuttingDown = true
        if let process, process.isRunning {
            process.terminate()
        }
        reset()
        failAll(BridgeError.processExited(status: 0,
                                          detail: "The Porte runtime was shut down."))
        isShuttingDown = false
    }

    // MARK: - Requests

    public func request(_ op: String,
                        args: [String: JSONValue] = [:],
                        onEvent: (@Sendable (BridgeEvent) -> Void)? = nil) async throws -> JSONValue {
        _ = try await start()
        guard let input, let process, process.isRunning else {
            throw BridgeError.notStarted
        }
        nextRequest += 1
        let id = "r\(nextRequest)"
        let document = JSONValue.object([
            "id": .string(id),
            "op": .string(op),
            "args": .object(args),
        ])
        var payload = try JSONEncoder().encode(document)
        payload.append(0x0A)
        return try await withCheckedThrowingContinuation { continuation in
            pending[id] = Pending(continuation: continuation, onEvent: onEvent)
            do {
                try input.write(contentsOf: payload)
            } catch {
                pending.removeValue(forKey: id)
                continuation.resume(throwing: error)
            }
        }
    }

    /// Ask the bridge to stop the current operation. The bridge replies to the
    /// synthetic id, which no request is waiting for; the original request then
    /// completes with a `cancelled` error or its natural result.
    public func cancel() {
        guard let input, process?.isRunning == true else { return }
        nextRequest += 1
        let id = "cancel-\(nextRequest)"
        let document = JSONValue.object([
            "id": .string(id),
            "op": .string("cancel"),
        ])
        guard var payload = try? JSONEncoder().encode(document) else { return }
        payload.append(0x0A)
        try? input.write(contentsOf: payload)
    }

    private final class LineBuffer: @unchecked Sendable {
        private var storage = Data()
        private let lock = NSLock()

        func append(_ data: Data) -> [String] {
            lock.lock()
            defer { lock.unlock() }
            storage.append(data)
            var lines: [String] = []
            while let index = storage.firstIndex(of: 0x0A) {
                let lineData = storage[storage.startIndex..<index]
                storage.removeSubrange(storage.startIndex...index)
                if let text = String(data: lineData, encoding: .utf8) {
                    lines.append(text)
                }
            }
            // A broken peer must not grow this without bound.
            if storage.count > 1 << 20 { storage.removeAll() }
            return lines
        }
    }

    public func diagnostics() -> String {
        var lines = [
            "runtime python: \(runtime.python)",
            "runtime root:   \(runtime.root)",
            "process running: \(process?.isRunning == true)",
            "bridge ready: \(ready != nil)",
        ]
        if let ready {
            lines.append("bridge version: \(ready.version) (protocol \(ready.protocolVersion))")
        }
        if !stderrTail.isEmpty {
            lines.append("engine output:")
            lines.append(contentsOf: stderrTail.suffix(80))
        }
        return lines.joined(separator: "\n")
    }
}

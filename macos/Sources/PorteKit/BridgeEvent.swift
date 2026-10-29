import Foundation

/// One message from the bridge.
///
/// Shaped after protocol 1 in `porte/gui/bridge.py`: progress is `state`,
/// `progress` or `log`; completion is `result`; failure is `error`. Everything
/// carries the id of the request it belongs to, except the initial `ready`.
public struct BridgeEvent: Equatable, Sendable {
    public let id: String?
    public let event: String
    public let state: String?
    public let message: String?
    public let fraction: Double?
    public let code: String?
    public let data: JSONValue?
    public let raw: [String: JSONValue]

    public var isResult: Bool { event == "result" }
    public var isError: Bool { event == "error" }
    public var isReady: Bool { event == "ready" }

    public init(id: String?, event: String, state: String? = nil,
                message: String? = nil, fraction: Double? = nil,
                code: String? = nil, data: JSONValue? = nil,
                raw: [String: JSONValue] = [:]) {
        self.id = id
        self.event = event
        self.state = state
        self.message = message
        self.fraction = fraction
        self.code = code
        self.data = data
        self.raw = raw
    }

    public static func decode(line: String) throws -> BridgeEvent {
        guard let bytes = line.data(using: .utf8) else {
            throw BridgeError.protocolViolation("bridge wrote a line that is not UTF-8")
        }
        let value: JSONValue
        do {
            value = try JSONDecoder().decode(JSONValue.self, from: bytes)
        } catch {
            throw BridgeError.protocolViolation(
                "bridge wrote a non-JSON line: \(line.prefix(180))")
        }
        guard let object = value.objectValue else {
            throw BridgeError.protocolViolation("bridge wrote a non-object message")
        }
        guard let event = object["event"]?.stringValue else {
            throw BridgeError.protocolViolation("bridge message has no event field")
        }
        let identifier = object["id"]?.stringValue
            ?? object["id"]?.intValue.map(String.init)
        return BridgeEvent(id: identifier,
                           event: event,
                           state: object["state"]?.stringValue,
                           message: object["message"]?.stringValue,
                           fraction: object["fraction"]?.doubleValue,
                           code: object["code"]?.stringValue,
                           data: object["data"],
                           raw: object)
    }
}

/// A failure reported by the bridge with a machine-readable code.
public struct BridgeFailure: LocalizedError, Equatable, Sendable {
    public let code: String
    public let message: String
    public let data: JSONValue?

    public init(code: String, message: String, data: JSONValue? = nil) {
        self.code = code
        self.message = message
        self.data = data
    }

    public var errorDescription: String? { message }
}

/// A failure of the bridge transport itself.
public enum BridgeError: LocalizedError, Equatable, Sendable {
    case notStarted
    case processExited(status: Int32, detail: String)
    case protocolViolation(String)
    case handshakeTimedOut(stderr: String)

    public var errorDescription: String? {
        switch self {
        case .notStarted:
            return "The Porte runtime is not running."
        case .processExited(let status, let detail):
            let suffix = detail.isEmpty ? "" : "\n\n\(detail)"
            return "The Porte runtime stopped unexpectedly (status \(status)).\(suffix)"
        case .protocolViolation(let detail):
            return "The Porte runtime sent an unreadable message. \(detail)"
        case .handshakeTimedOut(let stderr):
            let suffix = stderr.isEmpty ? "" : "\n\n\(stderr)"
            return "The Porte runtime did not become ready in time.\(suffix)"
        }
    }
}

import Foundation

/// Where the Porte engine lives, and how to start its bridge process.
///
/// The descriptor is written by `macos/build.sh` into the app bundle and can be
/// replaced by a runtime the user selects in setup. It mirrors the record a
/// generated application bundle uses for its own shared runtime.
public struct RuntimeDescriptor: Codable, Equatable, Sendable, Identifiable {
    public var schema: Int
    public var python: String
    public var root: String
    public var pythonPath: [String]
    public var version: String?

    public init(schema: Int = 1, python: String, root: String,
                pythonPath: [String]? = nil, version: String? = nil) {
        self.schema = schema
        self.python = python
        self.root = root
        self.pythonPath = pythonPath ?? [root]
        self.version = version
    }

    private enum CodingKeys: String, CodingKey {
        case schema, python, root, version
        case pythonPath = "pythonpath"
    }

    public var id: String { "\(python)|\(root)" }
    public var pythonURL: URL { URL(fileURLWithPath: python) }
    public var rootURL: URL { URL(fileURLWithPath: root) }

    /// An executable interpreter plus the package this app talks to.
    public var isValid: Bool {
        let manager = FileManager.default
        guard manager.isExecutableFile(atPath: python) else { return false }
        guard manager.fileExists(atPath: root) else { return false }
        let bridge = rootURL.appendingPathComponent("porte/gui/bridge.py").path
        return manager.fileExists(atPath: bridge)
    }

    public var healthProblem: String? {
        let manager = FileManager.default
        if !manager.isExecutableFile(atPath: python) {
            return "The recorded Python interpreter no longer exists at \(python)."
        }
        if !manager.fileExists(atPath: root) {
            return "The Porte package directory no longer exists at \(root)."
        }
        if !manager.fileExists(atPath: rootURL.appendingPathComponent("porte/gui/bridge.py").path) {
            return "\(root) does not contain the Porte GUI bridge."
        }
        return nil
    }
}

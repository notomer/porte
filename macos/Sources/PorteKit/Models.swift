import Foundation

// MARK: - Workflow

/// The real states of an import or run, as reported by the engine.
public enum WorkflowState: String, Codable, CaseIterable, Sendable {
    case idle
    case analyzing
    case preparing
    case installing
    case launching
    case running
    case completed
    case failed
    case unsupported
    case cancelled

    public init(bridgeState: String?) {
        switch bridgeState {
        case "analyzing": self = .analyzing
        case "preparing": self = .preparing
        case "installing", "bundling": self = .installing
        case "launching": self = .launching
        case "running": self = .running
        case "completed": self = .completed
        case "failed": self = .failed
        case "unsupported": self = .unsupported
        case "cancelled": self = .cancelled
        default: self = .idle
        }
    }

    public var isBusy: Bool {
        switch self {
        case .analyzing, .preparing, .installing, .launching, .running:
            return true
        case .idle, .completed, .failed, .unsupported, .cancelled:
            return false
        }
    }

    public var isFailure: Bool {
        self == .failed || self == .unsupported || self == .cancelled
    }

    public var title: String {
        switch self {
        case .idle: return "Ready"
        case .analyzing: return "Inspecting"
        case .preparing: return "Preparing"
        case .installing: return "Installing"
        case .launching: return "Launching"
        case .running: return "Running"
        case .completed: return "Finished"
        case .failed: return "Failed"
        case .unsupported: return "Not supported"
        case .cancelled: return "Cancelled"
        }
    }

    public var symbolName: String {
        switch self {
        case .idle: return "checkmark.circle"
        case .analyzing: return "magnifyingglass"
        case .preparing: return "wrench.and.screwdriver"
        case .installing: return "arrow.down.circle"
        case .launching: return "play.circle"
        case .running: return "play.circle"
        case .completed: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.triangle.fill"
        case .unsupported: return "nosign"
        case .cancelled: return "xmark.circle"
        }
    }
}

// MARK: - Runtime backends

/// One execution backend, exactly as the engine reports it.
///
/// Porte has two: its own experimental engine, and an external Wine runtime it
/// discovers but never downloads. The UI shows both because "can Porte run this
/// file?" has a different answer per backend.
public struct RuntimeBackendStatus: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var name: String
    public var available: Bool
    public var version: String?
    public var path: String?
    public var usableForGUI: Bool
    public var reasons: [String]
    public var warnings: [String]
    public var license: String
    public var requiresRosetta: Bool
    public var rosettaAvailable: Bool

    private enum CodingKeys: String, CodingKey {
        case id, name, available, version, path, reasons, warnings, license
        case usableForGUI = "usable_for_gui"
        case requiresRosetta = "requires_rosetta"
        case rosettaAvailable = "rosetta_available"
    }

    public var isCompatibilityRuntime: Bool { id == "wine" }

    public var statusLabel: String {
        if available { return "Ready" }
        return "Unavailable"
    }

    public var summary: String {
        if let version, !version.isEmpty {
            return available ? "\(name) \(version)" : "\(name) (unavailable)"
        }
        return name
    }
}

// MARK: - Hello

public struct HealthReport: Codable, Equatable, Sendable {
    public var engine: Bool
    public var modules: [String: Bool]
    public var clang: Bool
    public var iconutil: Bool
    public var writable: Bool
    public var launcherSource: Bool
    public var problems: [String]
    public var ok: Bool
    public var supportRoot: String

    public var summary: String {
        if ok { return "The compatibility runtime is ready." }
        if let first = problems.first { return first }
        return "The compatibility runtime needs attention."
    }
}

public struct HelloInfo: Codable, Equatable, Sendable {
    public var protocolVersion: Int
    public var schema: Int
    public var version: String
    public var python: String
    public var root: String
    public var supportRoot: String
    public var appsRoot: String
    public var health: HealthReport
    public var capabilities: [String: Bool]
    public var backends: [RuntimeBackendStatus]?
    public var host: JSONValue

    private enum CodingKeys: String, CodingKey {
        case schema, version, python, root, supportRoot, appsRoot
        case health, capabilities, host
        case protocolVersion = "protocol"
    }

    public var supportsBundles: Bool {
        capabilities["generate_bundles"] == true
    }
}

// MARK: - Analysis

public struct InstalledMatch: Codable, Equatable, Sendable {
    public var productKey: String?
    public var bundleId: String?
    public var name: String?
    public var version: String?
    public var kind: String?
}

public struct PackageFacts: Codable, Equatable, Sendable {
    public var format: String?
    public var version: String?
    public var manufacturer: String?
    public var publisher: String?
    public var productCode: String?
    public var files: Int?
    public var registryValues: Int?
    public var shortcuts: Int?
    public var applications: [String?]?
}

/// What the engine found in a file, before anything is installed or run.
public struct AnalysisReport: Codable, Equatable, Sendable {
    public var path: String
    public var name: String
    public var suffix: String
    public var kind: String
    public var supported: Bool
    public var installSupported: Bool
    public var runSupported: Bool
    public var profile: String?
    public var sha256: String?
    public var size: Int
    public var blockers: [String]
    public var warnings: [String]
    public var imports: [String]
    public var missingStubs: [String]
    public var notes: [String]
    public var iconPng: String?
    public var installed: InstalledMatch?
    public var package: PackageFacts?
    public var actions: [String]
    public var summary: String
    /// Which backend would execute this file, and why.
    public var runtime: String?
    public var runtimeName: String?
    public var backends: [AnalysisBackend]?

    public var fileURL: URL { URL(fileURLWithPath: path) }

    public var runtimeLabel: String? {
        guard let runtimeName else { return nil }
        if let backend = backends?.first(where: { $0.id == runtime }),
           let version = backend.version, !version.isEmpty {
            return "\(runtimeName) \(version)"
        }
        return runtimeName
    }

    /// The backend that would run this file, for the details view.
    public var selectedBackend: AnalysisBackend? {
        backends?.first { $0.id == runtime }
    }

    public var kindLabel: String {
        switch kind {
        case "exe-portable": return "Portable Windows application"
        case "exe-installer": return "Windows installer"
        case "exe-unsupported": return "Unsupported Windows executable"
        case "msi": return "Windows Installer package"
        case "msi-invalid": return "Unreadable Windows Installer package"
        case "msix": return "MSIX package"
        case "msix-invalid": return "Unreadable MSIX package"
        case "batch": return "Batch file"
        default: return "Unsupported file"
        }
    }

    public var canRunNow: Bool { runSupported && actions.contains("run") }
    public var canInstall: Bool { installSupported && actions.contains("install") }
}

/// One backend's verdict on one file, as reported by the analysis itself.
/// It is narrower than `RuntimeBackendStatus`: an analysis says whether this
/// particular file is supported, not whether the runtime is installed.
public struct AnalysisBackend: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var name: String?
    public var available: Bool?
    public var supported: Bool
    public var version: String?
    public var reason: String?
    public var blockers: [String]?
    public var warnings: [String]?

    public var isAvailable: Bool { available ?? true }
    public var summary: String {
        let label = name ?? id
        if supported { return version.map { "\(label) \($0)" } ?? label }
        return reason ?? "\(label) cannot run this file"
    }
}

public struct InstalledBundleRef: Codable, Equatable, Sendable {
    public var app: String
    public var bundleId: String?
    public var name: String?
    public var exe: String?
}

public struct SkippedExecutable: Codable, Equatable, Sendable {
    public var file: String
    public var missing: [String]
}

public struct InstallResult: Codable, Equatable, Sendable {
    public var kind: String
    public var name: String
    public var product: String?
    public var environment: String?
    public var bundles: [InstalledBundleRef]
    public var skippedExecutables: [SkippedExecutable]
    public var warnings: [String]
    public var elapsedMs: Double
    public var files: Int?
}

public struct RunResult: Codable, Equatable, Sendable {
    public var exitCode: Int
    public var fault: String?
    public var runMs: Double
    public var output: String
    public var outputBytes: Int
    public var apiCalls: [String: Int]
    public var dispatches: Int?
    public var profile: String?
}

// MARK: - Library

public struct InstalledApplication: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var bundleId: String
    public var name: String
    public var path: String
    public var profile: String?
    public var runs: Int
    public var status: String
    public var problem: String?
    public var stateDir: String?
    public var lastRun: String?
    public var productKey: String?
    public var arguments: [String]
    public var runtimeKind: String?

    public var isReady: Bool { status == "ready" }
    public var statusLabel: String {
        switch status {
        case "ready": return "Ready"
        case "runtime-missing": return "Runtime missing"
        case "damaged": return "Damaged"
        default: return status
        }
    }
    public var appURL: URL { URL(fileURLWithPath: path) }
}

public struct InstalledProduct: Codable, Equatable, Sendable, Identifiable {
    public var productKey: String
    public var name: String
    public var version: String?
    public var kind: String?
    public var manufacturer: String?
    public var installedAt: String?
    public var environment: String?
    public var files: Int?
    public var registryValues: Int?
    public var bundles: [InstalledBundleRef]
    public var skippedExecutables: [SkippedExecutable]
    public var warnings: [String]
    public var package: String?

    public var id: String { productKey }
    public var kindLabel: String {
        switch kind {
        case "msi": return "Windows Installer"
        case "msix": return "MSIX package"
        default: return "Windows program"
        }
    }
}

public struct LibrarySnapshot: Codable, Equatable, Sendable {
    public var applications: [InstalledApplication]
    public var products: [InstalledProduct]

    public init(applications: [InstalledApplication] = [], products: [InstalledProduct] = []) {
        self.applications = applications
        self.products = products
    }
}

// MARK: - Diagnostics

public struct ToolchainInfo: Codable, Equatable, Sendable {
    public var clang: String?
    public var clangPath: String?
    public var sdk: String?
}

public struct DiagnosticsReport: Codable, Equatable, Sendable {
    public var protocolVersion: Int
    public var version: String
    public var python: String
    public var root: String
    public var supportRoot: String
    public var applicationsRoot: String
    public var products: [JSONValue]
    public var cacheBytes: Int
    public var recentRuns: [JSONValue]
    public var health: HealthReport
    public var host: JSONValue
    public var toolchain: ToolchainInfo?

    private enum CodingKeys: String, CodingKey {
        case version, python, root, supportRoot, applicationsRoot, products
        case cacheBytes, recentRuns, health, host, toolchain
        case protocolVersion = "protocol"
    }
}
